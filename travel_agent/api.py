"""Map-facing API and a capability-scoped bridge for Flower AgentApp workers."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from travel_agent import intake
from travel_agent.agents import ExecutionBudget, ModelRunner, RulesRunner, openai_client
from travel_agent.coordinator import Coordinator, apply_event
from travel_agent.flower_backend import FlowerBackend
from travel_agent.providers.services import (
    ProviderError,
    fixture_supported,
    make_geocoder,
    make_provider,
    resolve_router,
)
from travel_agent.schemas import Contract, IntakeTurn, Proposal, TripEvent, TripRequest, TripSnapshot
from travel_agent.settings import ModelSettings, effective_model_calls, model_settings_from_env
from travel_agent.store import Conflict, NotFound, Store

WEB = Path(__file__).parent / "web"


class Geocodes(Protocol):
    """What the API needs from a geocoder: `search(query)` returning a GeoPlace or None."""
    def search(self, query: str): ...


class CreateTrip(Contract):
    request: TripRequest = Field(default_factory=TripRequest)


class SubmitEvent(Contract):
    base_revision: int = Field(ge=0)
    event: TripEvent


class WorkerEvent(Contract):
    type: str = Field(max_length=80, pattern=r"^[a-z_.]+$")
    data: dict


def create_app(db_path: str | None = None, *, data_mode: str | None = None,
               agent_mode: str | None = None, execution_backend: str | None = None,
               flower_backend: FlowerBackend | None = None, geocoder: Geocodes | None = None,
               intake_client=None) -> FastAPI:
    store = Store(db_path or os.getenv("TRAVEL_DB", "runtime/travel.sqlite3"))
    data_mode = data_mode or os.getenv("TRAVEL_DATA_MODE", "fixture")
    agent_mode = agent_mode or os.getenv("TRAVEL_AGENT_MODE", "rules")
    backend = execution_backend or os.getenv("TRAVEL_EXECUTION_BACKEND", "local")
    flower_mode = os.getenv("TRAVEL_FLOWER_MODE", "control")
    if (data_mode not in ("fixture", "live") or agent_mode not in ("rules", "model") or backend not in ("local", "flower")
            or flower_mode not in ("control", "bridge")):
        raise ValueError("Invalid data mode, agent mode, execution backend, or Flower mode")
    token = os.getenv("TRAVEL_API_TOKEN", "")
    public_url = os.getenv("TRAVEL_PUBLIC_URL", "").rstrip("/")
    if public_url and not token:
        raise ValueError("Set TRAVEL_API_TOKEN before enabling a public bridge URL")
    # Control mode: this server submits SuperGrid runs itself and reads their run events (no public URL).
    # Bridge mode: a SuperGrid worker calls back into a public HTTPS origin with a scoped job credential.
    flower = None
    if backend == "flower" and flower_mode == "control":
        if data_mode != "fixture":
            raise ValueError("SuperGrid workers plan on bundled fixture data; live providers are configured locally only")
        flower = flower_backend or FlowerBackend.from_env()
    bridge = backend == "flower" and flower is None
    # Local model path: one set of bounded model settings, read once so /api/config and every job agree.
    settings: ModelSettings | None = model_settings_from_env() if backend == "local" and agent_mode == "model" else None
    # Live data: resolve the router the way make_provider does so /api/config reports what a job will use, and
    # build the provider now on the local backend so a bad TRAVEL_ROUTER or endpoint fails at startup, not mid-job.
    provider = None
    router = resolve_router() if data_mode == "live" else None
    if data_mode == "live" and backend == "local":
        try:
            provider = make_provider(data_mode)
        except (ProviderError, ValueError) as exc:
            raise ValueError(f"Live data providers are misconfigured: {exc}") from exc
        if getattr(provider, "router", None) in ("osrm", "ors"):
            router = provider.router
    router_label = {"osrm": "OSRM", "ors": "OpenRouteService"}.get(router, router or "the configured router")
    live_notice = ("Live OpenStreetMap data. Prices and hours stay unknown unless OSM tags verify them; "
                   f"routes via {router_label}.")
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trip-worker")
    geocoder_lock = threading.Lock()

    def get_geocoder() -> Geocodes:
        """The injected geocoder, or one built from the environment on first use (never at startup)."""
        nonlocal geocoder
        with geocoder_lock:
            if geocoder is None:
                geocoder = make_geocoder()
            return geocoder

    intake_lock = threading.Lock()
    intake_state: dict = {"client": intake_client, "built": intake_client is not None}

    def get_intake_client():
        """The injected intake model client, or one built once from the local model settings; None = rules only.

        The SuperGrid backend has no model endpoint on this machine, so the intake stays rules-based there.
        """
        with intake_lock:
            if not intake_state["built"]:
                intake_state["built"] = True
                if settings is not None:
                    try:
                        intake_state["client"] = openai_client(os.getenv("TRAVEL_MODEL_BASE_URL", ""),
                                                               os.getenv("TRAVEL_MODEL_API_KEY", ""),
                                                               intake.MODEL_TIMEOUT_S, settings.api)
                    except (ValueError, RuntimeError) as exc:
                        print(f"[intake] model enrichment disabled: {exc}", file=sys.stderr, flush=True)
            return intake_state["client"]

    def model_settings_view() -> dict | None:
        """The active path's model settings: the SuperGrid backend's, the local model's, or none for rules."""
        active = getattr(flower, "settings", None) if flower is not None else settings
        return active.describe() if isinstance(active, ModelSettings) else None

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)

    app = FastAPI(title="OSM Travel Companion", version="0.2.3", lifespan=lifespan)
    app.state.store = store

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(NotFound)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(PermissionError)
    async def permission_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if not token and host not in ("127.0.0.1", "localhost", "testserver", "[", "::1"):
            return JSONResponse({"detail": "Configure TRAVEL_API_TOKEN before remote access"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin writes are disabled"}, status_code=403)
            body = await request.body()
            if len(body) > 1_000_000:
                return JSONResponse({"detail": "Request exceeds 1 MB"}, status_code=413)
        path = request.url.path
        if token and path.startswith("/api/") and path not in ("/api/config", "/api/login"):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("travel_session", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "API authentication required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.2.3"}

    @app.get("/api/config")
    def config():
        return {"data_mode": data_mode, "agent_mode": "model" if backend == "flower" else agent_mode,
                "execution_backend": backend, "flower_mode": flower_mode if backend == "flower" else None,
                "flower": flower.describe() if flower else None, "auth_required": bool(token),
                "tile_url": os.getenv("TRAVEL_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                "default_request": TripRequest().model_dump(mode="json"),
                "fixture_notice": "Synthetic Berlin scenario. Prices, hours, weather and direct-line routes are unverified fixtures.",
                "live_notice": live_notice, "geocoding": True, "router": router,
                "model": flower.model if flower else settings.model if settings else None,
                "model_api": settings.api if settings else None,
                "model_settings": model_settings_view(), "intake": True,
                "supported_cities": [intake.FIXTURE_CITY] if data_mode == "fixture" else None}

    @app.post("/api/login")
    async def login(request: Request):
        supplied = (await request.json()).get("token", "")
        if not isinstance(supplied, str) or not token or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Invalid token")
        response = JSONResponse({"ok": True})
        response.set_cookie("travel_session", token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=8 * 3600)
        return response

    @app.get("/api/geocode")
    def geocode(q: str = ""):
        """Resolve a typed place name to coordinates and a timezone. No match is a 404, never a guessed location."""
        text = q.strip()
        if not 2 <= len(text) <= 80:
            raise HTTPException(400, "Enter a place name of 2 to 80 characters")
        try:
            place = get_geocoder().search(text)
        except ProviderError:
            raise HTTPException(503, "Geocoding service unavailable") from None
        except ValidationError as exc:
            # A malformed upstream record is a provider fault, not a client error; its text stays in the server log.
            print(f"[geocode] unusable result for {text!r}: {str(exc)[:500]}", file=sys.stderr, flush=True)
            raise HTTPException(503, "Geocoding service unavailable") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)[:200]) from None
        if place is None:
            raise HTTPException(404, "No place matched that name")
        return {"query": place.query, "name": place.name, "country": place.country, "admin1": place.admin1,
                "lat": place.coordinate.lat, "lon": place.coordinate.lon, "timezone": place.timezone,
                "population": place.population, "fixture_supported": fixture_supported(place.coordinate),
                "source": {"provider": place.source.provider, "status": place.source.status}}

    @app.post("/api/intake")
    def intake_turn(body: IntakeTurn):
        """One conversational turn: parse the message onto the brief, check the city, ask for what is missing.

        The rules parser always runs; in local model mode the model may fill more of the brief and `engine`
        reports whether it contributed. `ready` with a `request` means the brief can be posted to /api/trips.
        """
        brief = intake.parse(body.message, body.brief)
        engine = "rules"
        client = get_intake_client()
        if client is not None:
            model_name = settings.model if settings is not None else os.getenv("TRAVEL_MODEL", "").strip()
            enriched = intake.enrich_with_model(body.message, body.brief, body.history, client, model_name,
                                                timeout_s=intake.MODEL_TIMEOUT_S)
            if enriched is not None:
                brief, contributed = intake.merge_briefs(brief, enriched)
                engine = "model" if contributed else engine
        notes: list[str] = []
        days = intake.multi_day_mention(body.message)
        if days:
            notes.append(f"This planner builds one day at a time, so I will plan a single day of your {days}-day trip.")
        check = intake.resolve_city(brief.city, lambda name: get_geocoder().search(name), data_mode)
        if check.status == "unresolved":
            brief = brief.model_copy(update={"city": None})  # an unknown name is not a usable answer
        elif check.place is not None and check.place.name != brief.city:
            brief = brief.model_copy(update={"city": check.place.name})  # the geocoder's canonical spelling
        if check.note:
            notes.append(check.note)
        missing = intake.missing_fields(brief)
        ready, request = False, None
        if check.supported and not missing:
            request = intake.to_trip_request(brief, TripRequest(), check.place)
            ready = request is not None
            reply = (f"Great, I have everything I need. Building your day in {request.city} now." if ready else
                     "Something about that day does not work yet: it must end after it starts and last at most "
                     "18 hours. Could you adjust the times?")
        elif not missing and check.status == "unavailable":
            reply = (f"I have everything except a confirmed location for {brief.city}. Send any message to retry "
                     "the lookup, or name another city.")
        else:
            reply = intake.next_question(brief, check, data_mode=data_mode) or ""
        return {"reply": reply, "brief": brief.model_dump(mode="json"), "missing": missing, "ready": ready,
                "request": request.model_dump(mode="json") if request else None, "engine": engine, "notes": notes}

    def execute(job_id: str):
        nonlocal provider
        try:
            store.start_job(job_id)
            payload = store.job_input(job_id)
            snapshot = TripSnapshot.model_validate(payload["snapshot"])
            event = TripEvent.model_validate(payload["event"]) if payload["event"] else None
            metrics: dict = {}
            def emit(typ, value):
                if typ == "flower.run.finished" and isinstance(value.get("metrics"), dict):
                    metrics.update(value["metrics"])
                store.append_event(job_id, typ, value)
            budget = ExecutionBudget()
            if flower is not None:
                # SuperGrid executes the specialists; the result is validated again by store.finish_job.
                proposal = flower.run_job(snapshot, event, emit)
            else:
                if settings is not None:
                    budget = ExecutionBudget(wall_time_s=settings.wall_time_s, max_model_calls=effective_model_calls(
                        settings.max_model_calls, settings.max_tool_turns))
                    client = openai_client(os.getenv("TRAVEL_MODEL_BASE_URL", ""), os.getenv("TRAVEL_MODEL_API_KEY", ""),
                                           settings.model_timeout_s, settings.api)
                    runner = ModelRunner(client, settings.model, emit, budget, settings.max_tool_turns,
                                         settings.reasoning_effort, max_output_tokens=settings.max_output_tokens)
                else:
                    runner = RulesRunner(emit, budget)
                if provider is None:
                    provider = make_provider(data_mode)
                proposal = Coordinator(provider, runner, emit, budget).plan(snapshot, event)
            store.finish_job(job_id, proposal)
            # Clicking Create trip authorizes the initial itinerary. Every later revision requires review.
            if snapshot.revision == 0 and event is None and proposal.proposed.itinerary.validation.valid:
                store.accept(proposal.id)
            # SuperGrid runs report the AgentApp's budget; local runs report their own. Every kind is always present.
            usage = {"model_calls": 0, "tool_calls": 0, "provider_calls": 0, **(metrics or budget.metrics())}
            emit("job.finished", {"job_id": job_id, "proposal_id": proposal.id, "metrics": usage})
        except Exception as exc:
            # Do not expose provider credentials or arbitrary model response text to API clients.
            allowed = isinstance(exc, (ValueError, Conflict, ProviderError, RuntimeError))
            message = str(exc)[:350] if allowed else type(exc).__name__ + ": inspect server logs locally"
            detail = f"{type(exc).__name__}: {str(exc)[:1500]}"
            for secret_name in ("TRAVEL_MODEL_API_KEY", "ORS_API_KEY", "TRAVEL_API_TOKEN"):
                value = os.getenv(secret_name, "")
                if value:
                    message = message.replace(value, "[redacted]")
                    detail = detail.replace(value, "[redacted]")
            # The full, redacted cause goes to the server log only; the client sees the short message.
            print(f"[job {job_id}] failed: {detail}", file=sys.stderr, flush=True)
            store.fail_job(job_id, message)

    def queue(snapshot, event=None):
        if bridge and not public_url:
            raise HTTPException(400, "Flower bridge requires TRAVEL_PUBLIC_URL and TRAVEL_API_TOKEN")
        job, capability = store.enqueue(snapshot, event, bridge)
        if capability is not None:
            if not bridge:
                pool.submit(execute, job["id"])
            else:
                run_config = " ".join(f"{k}={json.dumps(v)}" for k, v in {
                    "travel.api-url": public_url, "travel.job-id": job["id"], "travel.job-token": capability}.items())
                job["flower_command"] = "uv run flwr run . supergrid --run-config " + shlex.quote(run_config) + " --stream"
                job["capability_notice"] = "Private, single-job credential. Expires in 30 minutes; do not publish this command."
        return job

    @app.post("/api/trips")
    def create_trip(body: CreateTrip):
        snapshot = TripSnapshot(id=uuid4().hex, request=body.request, data_mode=data_mode,
                                agent_mode="model" if backend == "flower" else agent_mode)
        store.create_trip(snapshot)
        job = queue(snapshot)
        return {"trip_id": snapshot.id, "job": job}

    @app.get("/api/trips")
    def list_trips():
        return store.trip_list()

    @app.get("/api/trips/{trip_id}")
    def read_trip(trip_id: str):
        return {"trip": store.trip(trip_id).model_dump(mode="json"), "proposals": store.proposals(trip_id)}

    @app.get("/api/trips/{trip_id}/export")
    def export_trip(trip_id: str):
        snapshot = store.trip(trip_id)
        return JSONResponse(snapshot.model_dump(mode="json"), headers={
            "Content-Disposition": f'attachment; filename="itinerary-{snapshot.id[:12]}.json"'})

    @app.post("/api/trips/import")
    def import_trip(body: TripSnapshot):
        from travel_agent.planning.engine import validate
        if len(body.places) > 24 or body.itinerary and len(body.itinerary.stops) > 8:
            raise HTTPException(400, "Imported itinerary exceeds the MVP limits")
        if body.itinerary and not validate(body, body.itinerary).valid:
            raise HTTPException(400, "Imported itinerary failed validation")
        body.id = uuid4().hex
        store.create_trip(body)
        return {"trip_id": body.id}

    @app.post("/api/trips/{trip_id}/events")
    def submit_event(trip_id: str, body: SubmitEvent):
        snapshot = store.trip(trip_id)
        if snapshot.revision != body.base_revision:
            raise Conflict("Refresh the current trip revision before requesting changes")
        try:
            apply_event(snapshot, body.event)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        return {"trip_id": trip_id, "job": queue(snapshot, body.event)}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        return store.job(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def stream_events(job_id: str, request: Request, since: int = 0):
        store.job(job_id)
        header_id = request.headers.get("last-event-id", "0")
        try:
            cursor = max(since, int(header_id))
        except ValueError:
            cursor = since
        async def stream():
            nonlocal cursor
            for _ in range(4500):
                if await request.is_disconnected():
                    return
                events = store.events(job_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield f'id: {cursor}\ndata: {json.dumps(event)}\n\n'
                state = store.job(job_id)["status"]
                if state not in ("queued", "running"):
                    yield 'event: done\ndata: ' + json.dumps({"status": state}) + '\n\n'
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/proposals/{proposal_id}/accept")
    def accept(proposal_id: str):
        return store.accept(proposal_id).model_dump(mode="json")

    @app.post("/api/proposals/{proposal_id}/reject")
    def reject(proposal_id: str):
        store.reject(proposal_id)
        return {"ok": True}

    def worker_token(request: Request) -> str:
        return request.headers.get("authorization", "").removeprefix("Bearer ")

    @app.post("/internal/jobs/{job_id}/claim")
    def claim(job_id: str, request: Request):
        store.verify_worker(job_id, worker_token(request), claim=True)
        return store.job_input(job_id)

    @app.post("/internal/jobs/{job_id}/events")
    def append_worker_event(job_id: str, request: Request, body: WorkerEvent):
        store.verify_worker(job_id, worker_token(request))
        store.append_event(job_id, body.type, body.data)
        return {"ok": True}

    @app.post("/internal/jobs/{job_id}/result")
    def worker_result(job_id: str, request: Request, body: Proposal):
        store.verify_worker(job_id, worker_token(request))
        store.finish_job(job_id, body)
        payload = store.job_input(job_id)
        if body.base_revision == 0 and payload["event"] is None and body.proposed.itinerary.validation.valid:
            store.accept(body.id)
        store.append_event(job_id, "job.finished", {"job_id": job_id, "proposal_id": body.id})
        return {"ok": True}

    @app.post("/internal/jobs/{job_id}/failure")
    async def worker_failure(job_id: str, request: Request):
        store.verify_worker(job_id, worker_token(request))
        body = await request.json()
        store.fail_job(job_id, str(body.get("error", "Flower worker failed"))[:350])
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
    return app
