"""Map-facing API and a capability-scoped bridge for Flower AgentApp workers."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from travel_agent import intake
from travel_agent.agents import ExecutionBudget, ModelRunner, RulesRunner, openai_client
from travel_agent.coordinator import Coordinator, apply_event
from travel_agent.flower_backend import FlowerBackend
from travel_agent.providers.services import ProviderError, make_provider
from travel_agent.schemas import Contract, IntakeTurn, Proposal, TripEvent, TripRequest, TripSnapshot
from travel_agent.store import Conflict, NotFound, Store

WEB = Path(__file__).parent / "web"


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
               flower_backend: FlowerBackend | None = None) -> FastAPI:
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
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trip-worker")
    provider = None

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)

    app = FastAPI(title="OSM Travel Companion", version="0.2.0", lifespan=lifespan)
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
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/api/config")
    def config():
        return {"data_mode": data_mode, "agent_mode": "model" if backend == "flower" else agent_mode,
                "execution_backend": backend, "flower_mode": flower_mode if backend == "flower" else None,
                "flower": flower.describe() if flower else None, "auth_required": bool(token),
                "tile_url": os.getenv("TRAVEL_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                "default_request": TripRequest().model_dump(mode="json"),
                "fixture_notice": "Synthetic Berlin scenario. Prices, hours, weather and direct-line routes are unverified fixtures.",
                "model": flower.model if flower else (os.getenv("TRAVEL_MODEL", "") or None) if agent_mode == "model" else None,
                "supported_cities": sorted(intake.SUPPORTED_CITIES.values())}

    @app.post("/api/login")
    async def login(request: Request):
        supplied = (await request.json()).get("token", "")
        if not isinstance(supplied, str) or not token or not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Invalid token")
        response = JSONResponse({"ok": True})
        response.set_cookie("travel_session", token, httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", max_age=8 * 3600)
        return response

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
                if agent_mode == "model":
                    client = openai_client(os.getenv("TRAVEL_MODEL_BASE_URL", ""), os.getenv("TRAVEL_MODEL_API_KEY", ""),
                                           float(os.getenv("TRAVEL_MODEL_TIMEOUT_S", "120")))
                    runner = ModelRunner(client, os.getenv("TRAVEL_MODEL", ""), emit, budget,
                                         int(os.getenv("TRAVEL_MAX_TOOL_TURNS", "2")))
                else:
                    runner = RulesRunner(emit, budget)
                if provider is None:
                    provider = make_provider(data_mode)
                proposal = Coordinator(provider, runner, emit, budget).plan(snapshot, event)
            # Clicking Create trip authorizes the initial itinerary. Every later revision requires review.
            auto_accept = snapshot.revision == 0 and event is None
            store.finish_job(job_id, proposal, auto_accept=auto_accept)
            emit("job.finished", {"job_id": job_id, "proposal_id": proposal.id, "metrics": metrics or budget.metrics()})
        except Exception as exc:
            # Do not expose provider credentials or arbitrary model response text.
            allowed = isinstance(exc, (ValueError, Conflict, ProviderError, RuntimeError))
            message = str(exc)[:350] if allowed else type(exc).__name__ + ": inspect server logs locally"
            for secret_name in ("TRAVEL_MODEL_API_KEY", "ORS_API_KEY", "TRAVEL_API_TOKEN"):
                value = os.getenv(secret_name, "")
                if value:
                    message = message.replace(value, "[redacted]")
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

    @app.post("/api/intake")
    def intake_turn(body: IntakeTurn):
        brief = intake.parse(body.message, body.brief)
        missing = intake.missing_fields(brief)
        notes: list[str] = []
        ready = False
        request = None
        if brief.city and not intake.is_supported_city(brief.city):
            notes.append(f"'{brief.city}' isn't supported yet — only Berlin is available right now.")
            reply = intake.next_question(brief) or ""
        elif not missing:
            request = intake.to_trip_request(brief, TripRequest())
            ready = request is not None
            reply = ("Great — I have everything I need. Building your itinerary now."
                     if ready else "Something about that trip doesn't quite work yet — could you clarify?")
        else:
            reply = intake.next_question(brief) or ""
        return {"reply": reply, "brief": brief.model_dump(mode="json"), "missing": missing, "ready": ready,
                "request": request.model_dump(mode="json") if request else None, "engine": "rules", "notes": notes}

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
        from travel_agent.planning.engine import validate_trip
        stop_cap = 8 * body.request.days
        if len(body.places) > 24 * body.request.days or (body.itinerary and len(body.itinerary.stops) > stop_cap):
            raise HTTPException(400, "Imported itinerary exceeds the MVP limits")
        if body.itinerary and not validate_trip(body, body.itinerary).valid:
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
        payload = store.job_input(job_id)
        auto_accept = body.base_revision == 0 and payload["event"] is None
        store.finish_job(job_id, body, auto_accept=auto_accept)
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
