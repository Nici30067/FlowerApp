"""Flower 1.37 AgentApp entry point. Instantiate the SDK only inside the running app.

Inputs (`agent.input`):
  * "Plan the Berlin demonstration itinerary."      one planning step on the bundled fixture
  * "plan then rain then budget 30"                 a scripted scenario; valid revisions auto-commit between steps
  * JSON {"request": ...} / {"event": ...} / {"request": ..., "events": [...]}
  * JSON {"job": "<encoded>"}                        a job handed over by the local map application
  * help | status | export | approve | reject | diagnose
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

from travel_agent import jobcodec
from travel_agent.agents import ExecutionBudget, ModelRunner, openai_client
from travel_agent.coordinator import Coordinator
from travel_agent.planning.engine import validate
from travel_agent.providers.services import make_provider
from travel_agent.schemas import Proposal, TripEvent, TripRequest, TripSnapshot

app = AgentApp()
DEFAULT_MODEL = "flower-endeavor-v1.0"
PLAN_COMMANDS = ("plan the berlin demonstration itinerary.", "plan the berlin demonstration itinerary", "plan")
MAX_STEPS = 6
HELP = (
    "Start with: Plan the Berlin demonstration itinerary.\n"
    "Then use: rain; budget 30; walk 2 km; status; approve; reject; export.\n"
    "Chain a scenario in one run: plan then rain then walk 2 km\n"
    "For explicit configuration, send JSON containing request (TripRequest) or event (TripEvent).\n"
    "Data fixtures remain labeled. Model execution uses the configured Flower model."
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
    lines.append("Type approve to commit this revision, or reject to keep the current itinerary.")
    return "\n".join(lines)


def parse_steps(prompt: str) -> list[str | dict]:
    """A prompt is one command, a JSON object, or commands joined with 'then' or ';'."""
    if prompt.startswith("{"):
        payload = json.loads(prompt)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("JSON input must be a non-empty object")
        if set(payload) <= {"request", "events"}:
            steps: list[str | dict] = []
            if "request" in payload:
                steps.append({"request": payload["request"]})
            events = payload.get("events", [])
            if not isinstance(events, list):
                raise ValueError("events must be a list")
            steps.extend({"event": item} for item in events)
            return steps
        if set(payload) in ({"event"}, {"job"}):
            return [payload]
        raise ValueError("Supply request, event, request with events, or job")
    steps = [s for s in re.split(r"\s*(?:;|\bthen\b)\s*", prompt.strip(), flags=re.IGNORECASE) if s]
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A scenario may contain at most {MAX_STEPS} steps")
    return steps


def resolve_step(step: str | dict, current: TripSnapshot | None, data_mode: str):
    """Return (snapshot, event) for one step, or None when the step is not a known command."""
    def fresh(request: TripRequest) -> TripSnapshot:
        return TripSnapshot(id=uuid4().hex, request=request, data_mode=data_mode, agent_mode="model")
    if isinstance(step, dict):
        if "request" in step:
            return fresh(TripRequest.model_validate(step["request"])), None
        if not current:
            raise ValueError("Create an itinerary before sending an event")
        return current, TripEvent.model_validate(step["event"])
    command = step.strip().lower()
    if command in PLAN_COMMANDS:
        return current or fresh(TripRequest()), None
    if not current:
        return None
    if command == "rain":
        return current, TripEvent(id=uuid4().hex, kind="rain", simulated=True)
    if match := re.fullmatch(r"budget (\d+(?:\.\d{1,2})?)", command):
        return current, TripEvent(id=uuid4().hex, kind="budget_changed", payload={"budget_minor": int(Decimal(match[1]) * 100)})
    if match := re.fullmatch(r"walk (\d+(?:\.\d+)?) km", command):
        return current, TripEvent(id=uuid4().hex, kind="pace_changed", payload={"max_walking_m": int(Decimal(match[1]) * 1000)})
    return None


class Session:
    """Per-run model configuration. Every planning step receives a fresh, bounded execution budget."""
    def __init__(self, agent, context):
        self.agent = agent
        self.model = str(context.run_config.get("agent.model", DEFAULT_MODEL))
        self.max_tool_turns = int(context.run_config.get("agent.max-tool-turns", 0))
        self.reasoning_effort = str(context.run_config.get("agent.reasoning-effort", "low"))
        self.wall_time_s = int(context.run_config.get("agent.wall-time-s", 900))
        self.max_model_calls = int(context.run_config.get("agent.max-model-calls", 12))
        self.client = openai_client(os.environ.get("FLWR_RUNTIME_BASE_URL", ""), os.environ.get("FLWR_RUNTIME_API_KEY", ""),
                                    float(context.run_config.get("agent.model-timeout-s", 120)))
        self.connectors = agent.connectors if context.run_config.get("agent.public-web", False) else None
        self.emit = make_emit(agent)

    def plan(self, snapshot: TripSnapshot, event: TripEvent | None, emit=None) -> tuple[Proposal, ExecutionBudget]:
        budget = ExecutionBudget(wall_time_s=self.wall_time_s, max_model_calls=self.max_model_calls)
        emit = emit or self.emit
        runner = ModelRunner(self.client, self.model, emit, budget, self.max_tool_turns, self.reasoning_effort)
        provider = make_provider(snapshot.data_mode)
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


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    prompt = context.run_config.get("agent.input", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("agent.input must be non-empty")
    prompt = prompt.strip()
    record = context.state.get("travel")
    current = TripSnapshot.model_validate_json(record["snapshot"]) if record and record.get("snapshot") else None
    pending = Proposal.model_validate_json(record["proposal"]) if record and record.get("proposal") else None
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
            context.state["travel"] = ConfigRecord({"snapshot": current.model_dump_json(), "proposal": ""})
            publish(agent, f"Committed revision {current.revision}. Use export for the full structured itinerary.")
        elif command == "reject":
            context.state["travel"] = ConfigRecord({"snapshot": current.model_dump_json() if current else "", "proposal": ""})
            publish(agent, "Pending changes rejected. The committed itinerary is unchanged.")
        elif command == "export":
            publish(agent, "```json\n" + (current.model_dump_json(indent=2) if current else "null") + "\n```")
        else:
            publish(agent, f"Committed revision: {current.revision if current else 'none'}. "
                          f"Pending proposal: {pending.id if pending else 'none'}.\n" + HELP)
        return
    if command.startswith("diagnose") and not bridged:
        diagnose(agent, context)
        return
    session = Session(agent, context)
    if bridged:
        bridge_run(agent, context, session)
        return
    steps = parse_steps(prompt)
    if len(steps) == 1 and isinstance(steps[0], dict) and "job" in steps[0]:
        job_run(agent, session, str(steps[0]["job"]))
        return
    data_mode = str(context.run_config.get("travel.data-mode", "fixture"))
    proposal = None
    for index, step in enumerate(steps, 1):
        resolved = resolve_step(step, current, data_mode)
        if resolved is None:
            publish(agent, HELP)
            return
        current, event = resolved
        say(agent, f"\nStep {index} of {len(steps)}: {event.kind if event else 'initial plan'}"
                   f"{' (simulated scenario)' if event and event.simulated else ''}\n")
        proposal, budget = session.plan(current, event)
        say(agent, summary(proposal, budget) + "\n")
        if index < len(steps):
            if not proposal.proposed.itinerary.validation.valid:
                say(agent, "The scenario stops here: the proposal has unresolved hard constraints.\n")
                break
            # Scripted scenario steps commit valid revisions so the next step builds on them.
            current = proposal.proposed
            say(agent, f"Committed revision {current.revision} to continue the scenario.\n")
    context.state["travel"] = ConfigRecord({"snapshot": current.model_dump_json(),
                                            "proposal": proposal.model_dump_json() if proposal else ""})
    done(agent)
