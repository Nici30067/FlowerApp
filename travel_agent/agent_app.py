"""Flower 1.37 AgentApp entry point. Instantiate the SDK only inside the running app.

Inputs (`agent.input`):
  * "Plan a day in Tokyo tomorrow, art and coffee, budget 60"   a description in plain words: the intake keeps a
    brief across the runs of a series, asks for what is missing, geocodes the city and plans it
  * "Plan the Berlin demonstration itinerary." / plan            one planning step on the default central-Berlin day
  * "plan then rain then budget 30"                              a scripted scenario; valid revisions auto-commit
    between steps (a description may open the chain: "... budget 60 then rain")
  * step commands: plan | rain | refresh | budget N | walk N km (at most MAX_STEPS per run)
  * JSON {"request": ...} / {"event": ...} / {"request": ..., "events": [...]}
  * JSON {"job": "<encoded>"}                                    a job handed over by the local map application
  * help | status | export | approve | reject | diagnose

Data providers come from the run config only (`travel.*` in pyproject.toml): live OpenStreetMap services by
default (Open-Meteo geocoding, Overpass, OSRM, Open-Meteo weather) or the bundled Berlin fixture with
travel.data-mode="fixture". Every step is validated before the first model call, and the committed snapshot, the
pending proposal and the conversation brief are persisted in context.state["travel"] after each successful step.
"""
from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from flwr.agentapp import AgentApp, AgentSession
from flwr.app import ConfigRecord, Context
from pydantic import ValidationError

from travel_agent import intake, jobcodec
from travel_agent.agents import ExecutionBudget, ModelRunner, openai_client
from travel_agent.coordinator import Coordinator
from travel_agent.planning.engine import validate
from travel_agent.providers.services import ProviderError, make_geocoder, make_provider
from travel_agent.schemas import GeoPlace, Proposal, TripBrief, TripEvent, TripRequest, TripSnapshot
from travel_agent.settings import ProviderSettings

app = AgentApp()
DEFAULT_MODEL = "flower-endeavor-v1.0"
PLAN_COMMANDS = ("plan the berlin demonstration itinerary.", "plan the berlin demonstration itinerary", "plan")
MAX_STEPS = 6
# A description that names no time window is planned over this local-time window instead of asking for it, so a
# one-line request ("Plan a day in Tokyo tomorrow, art and coffee, budget 60") plans in a single run.
DEFAULT_WINDOW = ("10:00", "17:00")
FIELD_LABELS = {"city": "city", "date": "date", "time_window": "time window", "interests": "interests"}
# intake.resolve_request opens its note for an unknown place name with this phrase; the name is then dropped from
# the brief so that the next answer (also a bare lower-case one) is read as the city.
UNRESOLVED_MARKER = intake.UNRESOLVED_PLACE_MARKER
HELP = (
    "Start with a description: Plan a day in Tokyo tomorrow 10:00 to 17:00, art and coffee, budget 60.\n"
    "I ask for anything still missing (city, date, interests) in the next run; without a time window the day "
    f"runs {DEFAULT_WINDOW[0]} to {DEFAULT_WINDOW[1]}.\n"
    "Then use: rain; refresh; budget 30; walk 2 km; status; approve; reject; export.\n"
    "Chain a scenario in one run: plan then rain then walk 2 km (a description may open the chain).\n"
    "plan alone builds the default central-Berlin day. For explicit configuration, send JSON containing "
    "request (TripRequest) or event (TripEvent).\n"
    "Data: live OpenStreetMap services by default; travel.data-mode=\"fixture\" selects the labeled Berlin "
    "fixture. Model execution uses the configured Flower model."
)


def say(agent, text: str) -> None:
    agent.events.emit({"type": "response.output_text.delta", "delta": text})


def done(agent) -> None:
    agent.events.emit({"type": "response.completed"})


def publish(agent, text: str) -> None:
    say(agent, text)
    done(agent)


def make_emit(agent):
    """Forward every coordinator event as structured data and narrate the collaboration as text."""
    def emit(kind: str, data: dict) -> None:
        agent.events.emit({"type": "travel." + kind, "kind": kind, "data": data})
        if kind == "agent.started":
            say(agent, f"[{data['role']}] specialist started on {data.get('model') or data.get('engine')}\n")
        elif kind == "agent.completed":
            say(agent, f"[{data['role']}] {data['report']['summary']}\n")
        elif kind == "collaboration.message":
            say(agent, f"{data['sender']} -> {data['recipient']}: {data['summary']}\n")
        elif kind == "planner.fallback":
            say(agent, f"Planner fell back to the catalog: {data['reason']} "
                       f"({data['candidate_count']} candidates)\n")
        elif kind == "planner.repair":
            say(agent, f"Planner repair {'adopted' if data['adopted'] else 'declined'}: {data['reason']}\n")
        elif kind == "validation.completed":
            say(agent, f"Deterministic validation: {data['status']}\n")
    return emit


def summary(proposal: Proposal, budget: ExecutionBudget) -> str:
    state = proposal.proposed
    itinerary = state.itinerary
    zone = ZoneInfo(state.request.timezone)
    lines = [f"\nProposed revision {state.revision}: {itinerary.validation.status}",
             f"Data: {state.data_mode}. Agent execution: {state.agent_mode}."]
    for i, stop in enumerate(itinerary.stops, 1):
        start = stop.start.astimezone(zone).strftime("%H:%M")
        end = stop.end.astimezone(zone).strftime("%H:%M")
        tags = " [locked]" if stop.locked else ""
        lines.append(f"{i}. {start} to {end}: {stop.name}{tags}")
    lines += [f"Walking: {itinerary.walking_m} m. Accounted cost: {itinerary.cost_minor / 100:.2f} {state.request.currency}.",
              f"Unknown prices: {itinerary.unknown_cost_count}. Model calls: {budget.model_calls}.",
              f"Added: {', '.join(proposal.added) or 'none'}. Removed: {', '.join(proposal.removed) or 'none'}."]
    codes = list(dict.fromkeys(i.code for i in itinerary.validation.issues))
    if codes:
        lines.append("Validation notes: " + ", ".join(codes))
    if state.weather_override:
        # A scenario override replaces the provider forecast until a refresh (weather_updated) clears it.
        note = state.weather_override.source.note or state.weather_override.source.provider
        lines.append(f"Weather: simulated scenario override active ({note})")
    lines.append("Type approve to commit this revision, or reject to keep the current itinerary.")
    return "\n".join(lines)


def live_line(settings: ProviderSettings) -> str:
    """The provenance line that opens every plan on live data."""
    routes = "OSRM" if settings.router == "osrm" else "OpenRouteService"
    return f"Live data: OpenStreetMap via Overpass, {routes} routes, Open-Meteo weather ({settings.router})"


def split_steps(prompt: str) -> list[str]:
    """Text steps are joined with 'then' or ';'."""
    return [s for s in re.split(r"\s*(?:;|\bthen\b)\s*", prompt.strip(), flags=re.IGNORECASE) if s]


def parse_steps(prompt: str) -> list[str | dict]:
    """A prompt is one command, a JSON object, or commands joined with 'then' or ';'.

    Text and JSON scenarios share the MAX_STEPS bound: a JSON request and each of its events count as one step.
    """
    if prompt.startswith("{"):
        payload = json.loads(prompt)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("JSON input must be a non-empty object")
        if set(payload) in ({"event"}, {"job"}):
            steps: list[str | dict] = [payload]
        elif set(payload) <= {"request", "events"}:
            steps = [{"request": payload["request"]}] if "request" in payload else []
            events = payload.get("events", [])
            if not isinstance(events, list):
                raise ValueError("events must be a list")
            steps.extend({"event": item} for item in events)
        else:
            raise ValueError("Supply request, event, request with events, or job")
    else:
        steps = list(split_steps(prompt))
    if not steps:
        raise ValueError("No planning step found")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A scenario may contain at most {MAX_STEPS} steps")
    return steps


Step = tuple[str, TripRequest | TripEvent | None]


def classify_step(step: str | dict) -> Step | None:
    """Classify one step without running it: ("request", TripRequest), ("plan", None) or ("event", TripEvent).

    Malformed JSON shapes raise a ValueError; an unknown text command returns None so the caller shows HELP.
    """
    if isinstance(step, dict):
        if "request" in step:
            return "request", TripRequest.model_validate(step["request"])
        if "event" in step:
            return "event", TripEvent.model_validate(step["event"])
        raise ValueError("Supply request or event")
    command = step.strip().lower()
    if command in PLAN_COMMANDS:
        return "plan", None
    if command == "rain":
        return "event", TripEvent(id=uuid4().hex, kind="rain", simulated=True)
    if command == "refresh":
        # Clears a scenario override and re-reads the provider forecast; no caller-supplied forecast is accepted.
        return "event", TripEvent(id=uuid4().hex, kind="weather_updated")
    if match := re.fullmatch(r"budget (\d+(?:\.\d{1,2})?)", command):
        payload = {"budget_minor": int(Decimal(match[1]) * 100)}
        return "event", TripEvent(id=uuid4().hex, kind="budget_changed", payload=payload)
    if match := re.fullmatch(r"walk (\d+(?:\.\d+)?) km", command):
        payload = {"max_walking_m": int(Decimal(match[1]) * 1000)}
        return "event", TripEvent(id=uuid4().hex, kind="pace_changed", payload=payload)
    return None


def compile_steps(steps: list[str | dict], have_snapshot: bool) -> list[Step] | None:
    """Validate every step before the first model call.

    Returns None when a text step is not a known command (the caller publishes HELP). An event that would run
    before any itinerary exists, and any malformed request or event, raise a ValueError.
    """
    compiled: list[Step] = []
    for step in steps:
        item = classify_step(step)
        if item is None:
            return None
        if item[0] == "event" and not have_snapshot:
            raise ValueError("Create an itinerary before sending an event")
        have_snapshot = True
        compiled.append(item)
    return compiled


def load_state(context) -> tuple[TripSnapshot | None, Proposal | None, TripBrief]:
    """The committed snapshot, the pending proposal and the conversation brief persisted by an earlier run."""
    record = context.state.get("travel")
    if not record:
        return None, None, TripBrief()
    snapshot = TripSnapshot.model_validate_json(record["snapshot"]) if record.get("snapshot") else None
    proposal = Proposal.model_validate_json(record["proposal"]) if record.get("proposal") else None
    brief = TripBrief.model_validate_json(record["brief"]) if record.get("brief") else TripBrief()
    return snapshot, proposal, brief


def save_state(context, snapshot: TripSnapshot | None, proposal: Proposal | None, brief: TripBrief | None) -> None:
    """ConfigRecord values are strings: an absent snapshot, proposal or (empty) brief is stored as ""."""
    context.state["travel"] = ConfigRecord({
        "snapshot": snapshot.model_dump_json() if snapshot else "",
        "proposal": proposal.model_dump_json() if proposal else "",
        "brief": brief.model_dump_json() if brief and brief != TripBrief() else ""})


class LazyGeocoder:
    """Builds the geocoder on first use, so a fixture plan of Berlin never opens a connection.

    Answers are remembered per name, so the place that resolved a city is available for the provenance line
    without a second lookup.
    """
    def __init__(self, settings: ProviderSettings):
        self.settings, self.geocoder = settings, None
        self.places: dict[str, GeoPlace | None] = {}

    def search(self, name: str) -> GeoPlace | None:
        if self.geocoder is None:
            self.geocoder = make_geocoder(settings=self.settings)
        self.places[name] = place = self.geocoder.search(name)
        return place


class Session:
    """Per-run model and provider configuration. Every planning step receives a fresh, bounded execution budget."""
    def __init__(self, agent, context, settings: ProviderSettings | None = None):
        config = context.run_config
        self.agent = agent
        self.model = str(config.get("agent.model", DEFAULT_MODEL))
        self.max_tool_turns = int(config.get("agent.max-tool-turns", 0))
        self.reasoning_effort = str(config.get("agent.reasoning-effort", "low"))
        self.wall_time_s = int(config.get("agent.wall-time-s", 900))
        configured_calls = int(config.get("agent.max-model-calls", 0))
        # 0 = auto: up to five specialist runs per step, each allowed its tool turns plus a final answer and one repair.
        self.max_model_calls = configured_calls if configured_calls > 0 else 5 * (self.max_tool_turns + 2)
        self.max_output_tokens = int(config.get("agent.max-output-tokens", 2000))
        public_web = bool(config.get("agent.public-web", False))
        if public_web and self.max_tool_turns == 0:
            raise ValueError("agent.public-web requires agent.max-tool-turns >= 1")
        self.client = openai_client(os.environ.get("FLWR_RUNTIME_BASE_URL", ""), os.environ.get("FLWR_RUNTIME_API_KEY", ""),
                                    float(config.get("agent.model-timeout-s", 120)))
        self.connectors = agent.connectors if public_web else None
        self.emit = make_emit(agent)
        # Inside a Flower run there are no provider environment variables: everything comes from travel.* keys.
        self.settings = settings or ProviderSettings.from_run_config(config)

    def budget(self) -> ExecutionBudget:
        """A fresh per-step budget; the cap is either the configured value or the auto rule above."""
        return ExecutionBudget(wall_time_s=self.wall_time_s, max_model_calls=self.max_model_calls)

    def fresh(self, request: TripRequest) -> TripSnapshot:
        return TripSnapshot(id=uuid4().hex, request=request, data_mode=self.settings.data_mode, agent_mode="model")

    def plan(self, snapshot: TripSnapshot, event: TripEvent | None, emit=None) -> tuple[Proposal, ExecutionBudget]:
        budget = self.budget()
        emit = emit or self.emit
        runner = ModelRunner(self.client, self.model, emit, budget, self.max_tool_turns, self.reasoning_effort,
                             self.max_output_tokens)
        # The snapshot's data mode wins (a job from the map application carries its own); the endpoints,
        # contact string and router come from the run config.
        provider = make_provider(mode=snapshot.data_mode, settings=self.settings)
        return Coordinator(provider, runner, emit, budget, self.connectors).plan(snapshot, event), budget


def diagnose(agent, context) -> None:
    """Probe the runtime model endpoint once per candidate ID and report latency. No planning occurs."""
    timeout_s = float(context.run_config.get("agent.model-timeout-s", 120))
    raw = str(context.run_config.get("agent.model-candidates", ""))
    candidates = [m.strip() for m in raw.split(",") if m.strip()] or [str(context.run_config.get("agent.model", DEFAULT_MODEL))]
    client = openai_client(os.environ.get("FLWR_RUNTIME_BASE_URL", ""), os.environ.get("FLWR_RUNTIME_API_KEY", ""),
                           timeout_s)
    lines = [f"Model diagnostics (timeout {timeout_s:.0f}s per call)"]
    for model in candidates[:6]:
        started = time.monotonic()
        try:
            response = client.responses.create(model=model, input="Reply with the single word OK.", max_output_tokens=32)
            text = (response.output_text or "").strip()[:80]
            lines.append(f"{model}: ok in {time.monotonic() - started:.1f}s; returned model={getattr(response, 'model', None)!r};"
                         f" text={text!r}")
        except Exception as exc:  # Report the failure class; the message is truncated and never includes credentials.
            lines.append(f"{model}: {type(exc).__name__} after {time.monotonic() - started:.1f}s: {str(exc)[:200]}")
        print(lines[-1], flush=True)
    publish(agent, "\n".join(lines))


def job_run(agent, session: Session, blob: str) -> None:
    """Execute one job handed over by the map application and return the proposal as a run event."""
    payload = jobcodec.decode(blob)
    snapshot = TripSnapshot.model_validate(payload["snapshot"])
    event = TripEvent.model_validate(payload["event"]) if payload.get("event") else None
    say(agent, f"Planning job for trip {snapshot.id[:8]} (revision {snapshot.revision}, trigger "
               f"{event.kind if event else 'initial_plan'}) on {session.model}.\n")
    try:
        proposal, budget = session.plan(snapshot, event)
    except Exception as exc:
        # The failure class and a truncated message are returned; provider credentials are never part of either.
        agent.events.emit({"type": "travel.failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        raise
    agent.events.emit({"type": "travel.result", "proposal": jobcodec.encode({"proposal": proposal.model_dump(mode="json")}),
                       "metrics": budget.metrics()})
    publish(agent, summary(proposal, budget))


def bridge_run(agent, context, session: Session):
    """Legacy bridge: claim a scoped job over HTTPS and post results back. Requires a public API origin."""
    config = context.run_config
    url = str(config.get("travel.api-url", "")).rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The Flower bridge requires an operator-configured HTTPS API origin")
    job_id = str(config.get("travel.job-id", ""))
    capability = str(config.get("travel.job-token", ""))
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not capability:
        raise ValueError("Missing or invalid scoped job credentials")
    # This credential authorizes only this job. Never send it to the model or connectors.
    with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + capability},
                      timeout=15, follow_redirects=False) as bridge:
        response = bridge.post(f"/internal/jobs/{job_id}/claim")
        response.raise_for_status()
        payload = response.json()
        original = TripSnapshot.model_validate(payload["snapshot"])
        event = TripEvent.model_validate(payload["event"]) if payload.get("event") else None
        def emit(kind, data):
            result = bridge.post(f"/internal/jobs/{job_id}/events", json={"type": kind, "data": data})
            result.raise_for_status()
            session.emit(kind, data)
        try:
            proposal, budget = session.plan(original, event, emit)
            response = bridge.post(f"/internal/jobs/{job_id}/result", json=proposal.model_dump(mode="json"))
            response.raise_for_status()
            publish(agent, summary(proposal, budget) + "\nReview and apply subsequent revisions in the map interface.")
        except Exception as exc:
            # Short type-only errors cannot leak the scoped token or provider secrets.
            bridge.post(f"/internal/jobs/{job_id}/failure", json={"error": type(exc).__name__ + ": Flower execution failed"})
            raise


def describe_brief(brief: TripBrief) -> str:
    """What the conversation has collected so far, in one clause; empty for an empty brief."""
    parts = []
    if brief.city:
        parts.append(f"city {brief.city}")
    if brief.date:
        parts.append(f"date {brief.date}")
    if brief.start_time and brief.end_time:
        parts.append(f"{brief.start_time} to {brief.end_time}")
    if brief.interests:
        parts.append("interests " + ", ".join(brief.interests))
    if brief.budget_minor is not None:
        parts.append(f"budget {brief.budget_minor / 100:g}")
    if brief.max_walking_m is not None:
        parts.append(f"walking {brief.max_walking_m / 1000:g} km")
    if brief.transport_mode:
        parts.append(brief.transport_mode)
    if brief.target_stops:
        parts.append(f"{brief.target_stops} stops")
    return "; ".join(parts)


def needed_hint(brief: TripBrief, missing: list[str]) -> str:
    known = describe_brief(brief)
    return (f"Still needed: {', '.join(FIELD_LABELS.get(m, m) for m in missing)}."
            + (f" So far: {known}." if known else "") + " Send help for the command list.")


def request_line(request: TripRequest, place: GeoPlace | None) -> str:
    zone = ZoneInfo(request.timezone)
    start, end = request.start.astimezone(zone), request.end.astimezone(zone)
    located = ""
    if place is not None:
        where = f"{place.name}, {place.country}" if place.country else place.name
        located = f" Located {where} via {place.source.provider}."
    return (f"Planning {request.title}: {start:%Y-%m-%d %H:%M} to {end:%H:%M} ({request.timezone}); interests "
            f"{', '.join(request.interests)}; budget {request.budget_minor / 100:.2f} {request.currency}; walking up to "
            f"{request.max_walking_m} m; start {request.origin.lat:.4f}, {request.origin.lon:.4f}.{located}\n")


def converse(agent, context, prompt: str, segments: list[str], current: TripSnapshot | None,
             pending: Proposal | None, brief: TripBrief) -> None:
    """A description in plain words, optionally followed by scripted steps ("... budget 60 then rain").

    The message is merged onto the persisted brief. A brief with gaps gets one question and is kept for the next
    run; a complete brief is geocoded, turned into a TripRequest and planned, and the brief is cleared.
    """
    message, tail = segments[0], compile_steps(segments[1:], True) if len(segments) > 1 else []
    if tail is None:  # a later segment is no command either: the whole prompt is one description
        message, tail = prompt, []
    if len(tail) + 1 > MAX_STEPS:
        raise ValueError(f"A scenario may contain at most {MAX_STEPS} steps")
    settings = ProviderSettings.from_run_config(context.run_config)
    try:
        brief = intake.parse(message, brief)
    except ValidationError:
        publish(agent, "I could not read that description.\n" + HELP)
        return
    notes = []
    days = intake.multi_day_mention(message)
    if days:
        notes.append(f"This planner builds one day at a time, so I will plan one day of your {days}-day trip.")
    missing = intake.missing_fields(brief)
    if missing == ["time_window"]:
        brief = brief.model_copy(update={"start_time": DEFAULT_WINDOW[0], "end_time": DEFAULT_WINDOW[1]})
        notes.append(f"No time window given: planning {DEFAULT_WINDOW[0]} to {DEFAULT_WINDOW[1]} local time "
                     "(say for example 9 to 18 to change it).")
        missing = []
    skipped = f"\nThe {len(tail)} scripted step(s) after the description were not run." if tail else ""
    preface = "".join(note + "\n" for note in notes)
    if missing:
        save_state(context, current, pending, brief)
        question = intake.next_question(brief, data_mode=settings.data_mode) or ""
        publish(agent, preface + question + "\n" + needed_hint(brief, missing) + skipped)
        return
    geocoder = LazyGeocoder(settings)
    request, reason = intake.resolve_request(brief, geocoder, settings.data_mode, TripRequest())
    if request is None:
        if reason and UNRESOLVED_MARKER in reason:
            brief = brief.model_copy(update={"city": None})
        save_state(context, current, pending, brief)
        publish(agent, preface + (reason or "That day cannot be planned yet.") + skipped)
        return
    session = Session(agent, context, settings)
    place = geocoder.places.get(brief.city.strip())  # None when the fixture city needed no lookup
    # The complete brief is stored before the first provider or model call: if a live service fails, the next
    # message of the series re-plans it instead of asking for the whole description again.
    save_state(context, current, pending, brief)
    say(agent, preface + request_line(request, place))
    run_steps(agent, context, session, [("plan", None), *tail], session.fresh(request), None)


def run_steps(agent, context, session: Session, compiled: list[Step], current: TripSnapshot | None,
              brief: TripBrief | None) -> None:
    """Run validated steps in order, persisting after each one; scripted steps auto-commit valid revisions."""
    for index, (kind, payload) in enumerate(compiled, 1):
        event = None
        if kind == "request":
            current = session.fresh(payload)
        elif kind == "plan":
            current = current or session.fresh(TripRequest())
        else:
            event = payload
        if index == 1 and current.data_mode == "live":
            say(agent, "\n" + live_line(session.settings) + "\n")
        say(agent, f"\nStep {index} of {len(compiled)}: {event.kind if event else 'initial plan'}"
                   f"{' (simulated scenario)' if event and event.simulated else ''}\n")
        try:
            proposal, budget = session.plan(current, event)
        except ProviderError as exc:
            # Live services are shared public instances; a failure is named and the run fails, nothing falls back.
            say(agent, f"Data provider failure: {exc}. This step was not planned; send the request again in a "
                       "moment, or use travel.data-mode=\"fixture\" for the bundled Berlin day.\n")
            raise
        say(agent, summary(proposal, budget) + "\n")
        # Each successful step is persisted at once, so a failure in a later step keeps everything before it.
        save_state(context, current, proposal, brief)
        if index < len(compiled):
            if not proposal.proposed.itinerary.validation.valid:
                say(agent, "The scenario stops here: the proposal has unresolved hard constraints.\n")
                break
            # Scripted scenario steps commit valid revisions so the next step builds on them.
            current = proposal.proposed
            save_state(context, current, None, brief)
            say(agent, f"Committed revision {current.revision} to continue the scenario.\n")
    done(agent)


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    prompt = context.run_config.get("agent.input", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent.input must be non-empty")
    prompt = prompt.strip()
    try:  # every travel.* key is checked once here, before any state is read or model call is made
        ProviderSettings.from_run_config(context.run_config)
    except ValueError as exc:
        publish(agent, f"Run-config error: {exc}. Check the travel.* keys (for example "
                       "travel.allow-public-overpass=true) and start the run again.")
        return
    current, pending, brief = load_state(context)
    command = prompt.lower()
    bridged = bool(context.run_config.get("travel.job-id"))
    if command in ("help", "status", "export", "approve", "reject") and not bridged:
        if command == "help":
            publish(agent, HELP)
        elif command == "approve":
            if not pending or (current and current.revision != pending.base_revision):
                raise ValueError("There is no current pending proposal")
            if not pending.proposed.itinerary.validation.valid or not validate(pending.proposed, pending.proposed.itinerary).valid:
                raise ValueError("The proposal has unresolved hard constraints")
            current = pending.proposed
            save_state(context, current, None, brief)
            publish(agent, f"Committed revision {current.revision}. Use export for the full structured itinerary.")
        elif command == "reject":
            if current or pending:
                save_state(context, current, None, brief)
            publish(agent, "Pending changes rejected. The committed itinerary is unchanged.")
        elif command == "export":
            publish(agent, "```json\n" + (current.model_dump_json(indent=2) if current else "null") + "\n```")
        else:
            settings = ProviderSettings.from_run_config(context.run_config)
            publish(agent, f"Committed revision: {current.revision if current else 'none'}. "
                          f"Pending proposal: {pending.id if pending else 'none'}. "
                          f"Conversation brief: {describe_brief(brief) or 'none'}. "
                          f"Data mode: {settings.data_mode} ({settings.router}).\n" + HELP)
        return
    if command.startswith("diagnose") and not bridged:
        diagnose(agent, context)
        return
    if bridged:
        bridge_run(agent, context, Session(agent, context))
        return
    # Text whose first step is no command is a description in plain words (JSON never is).
    segments = [] if prompt.startswith("{") else split_steps(prompt)
    if segments and classify_step(segments[0]) is None:
        converse(agent, context, prompt, segments, current, pending, brief)
        return
    steps = parse_steps(prompt)
    if len(steps) == 1 and isinstance(steps[0], dict) and "job" in steps[0]:
        job_run(agent, Session(agent, context), str(steps[0]["job"]))
        return
    # Every step is classified and validated up front, so a typo in step 3 costs no model calls in steps 1 and 2.
    compiled = compile_steps(steps, current is not None)
    if compiled is None:
        publish(agent, HELP)
        return
    run_steps(agent, context, Session(agent, context), compiled, current, brief)
