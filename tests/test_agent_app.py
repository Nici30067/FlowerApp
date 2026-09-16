"""AgentApp entry point, model output tolerance, job transport and bundle configuration."""
import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from travel_agent import agent_app, jobcodec
from travel_agent.agents import ExecutionBudget, ModelRunner, ToolDispatcher, extract_json_object
from travel_agent.schemas import Proposal, TripEvent, TripSnapshot

ROOT = Path(__file__).resolve().parents[1]


def report(role, ids, requests=()):
    return {"role": role, "summary": "Mock protocol response for testing", "candidate_ids": ids,
            "avoid_ids": [], "evidence_ids": [], "requests": list(requests)}


class FakeModel:
    """Answers every specialist with a valid report derived from the supplied context. Zero network calls.

    `fail_when(context)` simulates a model outage for the specialist calls it selects (a later scenario step).
    """
    def __init__(self, prose=False):
        self.prose, self.calls = prose, []
        self.fail_when = None
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        role = re.search(r"You are the (\w+) specialist", kwargs["instructions"])[1]
        context = json.loads(kwargs["input"][0]["content"])
        if self.fail_when and self.fail_when(context):
            raise RuntimeError("simulated model outage")
        places = context["places"]
        ids = [p["id"] for p in places]
        requests = []
        if role == "conditions" and any(i["precipitation_probability_pct"] >= 60 for i in context["weather"]["intervals"]):
            outdoor = [p["id"] for p in places if p["indoor"] is not True]
            requests.append({"sender": "conditions", "recipient": "discovery", "kind": "request",
                             "summary": "Return indoor alternatives.", "place_ids": outdoor, "evidence_ids": []})
        if role == "discovery" and any(m["sender"] == "conditions" for m in context["shared_messages"]):
            ids.sort(key=lambda pid: next(p["indoor"] is not True for p in places if p["id"] == pid))
        text = json.dumps(report(role, ids, requests))
        if self.prose:
            text = "I reviewed the candidates.\n```json\n" + text + "\n```\nLet me know if you need more."
        return SimpleNamespace(output=[], output_text=text, model="fake-model")


class FakeEvents:
    def __init__(self): self.items = []
    def emit(self, event): self.items.append(event)
    def get_trace(self): return []


class FakeAgent:
    def __init__(self):
        self.events = FakeEvents()
        self.connectors = None


def make_context(**overrides):
    config = {"agent.input": "plan", "agent.model": "fake-model", "agent.max-tool-turns": 0,
              "agent.model-timeout-s": 30, "agent.wall-time-s": 120, "travel.data-mode": "fixture"}
    config.update(overrides)
    return SimpleNamespace(run_config=config, state={})


@pytest.fixture
def runtime(monkeypatch):
    model = FakeModel()
    monkeypatch.setenv("FLWR_RUNTIME_BASE_URL", "https://runtime.invalid/v1/runtime")
    monkeypatch.setenv("FLWR_RUNTIME_API_KEY", "test-key")
    monkeypatch.setattr(agent_app, "openai_client", lambda base_url, api_key, timeout_s=120: model)
    return model


@pytest.mark.parametrize("text", [
    '{"role": "discovery", "candidate_ids": []}',
    'Here is my report:\n```json\n{"role": "discovery", "candidate_ids": []}\n```\nDone.',
    'I evaluated all candidates {"role": "discovery", "candidate_ids": []} and that is all.',
    '```\n{"role": "discovery", "candidate_ids": [], "requests": [{"kind": "request"}]}\n```',
])
def test_extract_json_object_tolerates_prose_and_fences(text):
    assert extract_json_object(text)["role"] == "discovery"


def test_extract_json_object_rejects_non_report_text():
    assert extract_json_object("no json here") is None
    assert extract_json_object('{"other": 1}') is None


def test_model_runner_repairs_one_invalid_final_answer(baseline):
    pid = baseline.places[0].id
    answers = [SimpleNamespace(output=[], output_text="Sure! Here is a summary without JSON."),
               SimpleNamespace(output=[], output_text=json.dumps(report("discovery", [pid])))]
    api = SimpleNamespace(create=lambda **kwargs: answers.pop(0))
    events = []
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: events.append(t), ExecutionBudget(), 0)
    result = runner.run("discovery", baseline, ToolDispatcher(baseline))
    assert result.candidate_ids == [pid]
    assert events.count("model.repair") == 1 and events.count("model.completed") == 2


def test_model_runner_gives_up_after_one_repair(baseline):
    api = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(output=[], output_text="still prose"))
    runner = ModelRunner(SimpleNamespace(responses=api), "test-model", lambda t, d: None, ExecutionBudget(), 0)
    with pytest.raises(ValueError, match="valid specialist report"):
        runner.run("discovery", baseline, ToolDispatcher(baseline))


def test_parse_steps_supports_commands_json_and_scenarios():
    assert agent_app.parse_steps("plan then rain; budget 30") == ["plan", "rain", "budget 30"]
    assert agent_app.parse_steps('{"request": {"title": "x"}, "events": [{"id": "a", "kind": "rain", "simulated": true}]}') == [
        {"request": {"title": "x"}}, {"event": {"id": "a", "kind": "rain", "simulated": True}}]
    assert agent_app.parse_steps('{"job": "abc"}') == [{"job": "abc"}]
    with pytest.raises(ValueError):
        agent_app.parse_steps('{"job": "abc", "request": {}}')
    with pytest.raises(ValueError):
        agent_app.parse_steps(" then ".join(["plan"] * 7))


def test_parse_steps_rejects_empty_and_oversized_inputs():
    for prompt in (";", "then", "; then ;", '{"events": []}'):
        with pytest.raises(ValueError, match="No planning step found"):
            agent_app.parse_steps(prompt)
    event = {"id": "e", "kind": "rain", "simulated": True}
    # The JSON path shares the text path's bound: the request and each event count as one step.
    assert len(agent_app.parse_steps(json.dumps({"request": {}, "events": [event] * 5}))) == agent_app.MAX_STEPS
    with pytest.raises(ValueError, match="at most 6 steps"):
        agent_app.parse_steps(json.dumps({"request": {}, "events": [event] * 6}))
    with pytest.raises(ValueError, match="at most 6 steps"):
        agent_app.parse_steps(json.dumps({"events": [event] * 7}))


def test_seven_json_events_are_rejected_before_any_model_call(runtime):
    event = {"id": "e", "kind": "rain", "simulated": True}
    payload = {"request": {}, "events": [event] * 7}
    agent, context = FakeAgent(), make_context(**{"agent.input": json.dumps(payload)})
    with pytest.raises(ValueError, match="at most 6 steps"):
        agent_app.main(agent, context)
    assert runtime.calls == [] and "travel" not in context.state


def test_empty_scenario_raises_without_touching_state(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": ";"})
    with pytest.raises(ValueError, match="No planning step found"):
        agent_app.main(agent, context)
    assert "travel" not in context.state and runtime.calls == []
    # With a committed snapshot and a pending proposal in place, an empty run leaves both exactly as they were.
    context.run_config["agent.input"] = "plan"
    agent_app.main(FakeAgent(), context)
    record = context.state["travel"]
    before = (record["snapshot"], record["proposal"])
    assert before[1]
    calls = len(runtime.calls)
    for prompt in (";", "then", '{"events": []}'):
        context.run_config["agent.input"] = prompt
        with pytest.raises(ValueError, match="No planning step found"):
            agent_app.main(FakeAgent(), context)
    record = context.state["travel"]
    assert (record["snapshot"], record["proposal"]) == before and len(runtime.calls) == calls


def test_unknown_step_anywhere_in_a_scenario_shows_help_before_any_model_call(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "plan then rani"})
    agent_app.main(agent, context)
    assert runtime.calls == [] and "travel" not in context.state
    assert agent.events.items[0]["delta"].startswith("Start with") and "refresh" in agent.events.items[0]["delta"]
    assert agent.events.items[-1]["type"] == "response.completed"


def test_event_before_any_itinerary_is_rejected_before_model_calls(runtime):
    event = json.dumps({"event": {"id": "e", "kind": "rain", "simulated": True}})
    for prompt in ("rain", "refresh then plan", event):
        agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
        with pytest.raises(ValueError, match="Create an itinerary before sending an event"):
            agent_app.main(agent, context)
        assert runtime.calls == [] and "travel" not in context.state
    # A malformed event is rejected up front as well, even when it comes after a valid planning step.
    bad = json.dumps({"request": {}, "events": [{"id": "e", "kind": "not-a-kind"}]})
    agent, context = FakeAgent(), make_context(**{"agent.input": bad})
    with pytest.raises(ValueError):
        agent_app.main(agent, context)
    assert runtime.calls == [] and "travel" not in context.state


def test_scenario_plan_then_rain_commits_between_steps(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "plan then rain"})
    agent_app.main(agent, context)
    types = [e["type"] for e in agent.events.items]
    assert types.count("travel.coordinator.started") == 2 and types[-1] == "response.completed"
    # Four specialists per step; in the rain step Conditions speaks first so Discovery consumes its request directly.
    assert types.count("travel.agent.completed") >= 8
    handoffs = [e for e in agent.events.items if e["type"] == "travel.collaboration.message" and e["data"]["kind"] == "request"]
    assert any(m["data"]["sender"] == "conditions" and m["data"]["recipient"] == "discovery" for m in handoffs)
    record = context.state["travel"]
    committed = TripSnapshot.model_validate_json(record["snapshot"])
    pending = Proposal.model_validate_json(record["proposal"])
    assert committed.revision == 1 and pending.base_revision == 1 and pending.trigger == "rain"
    assert all(committed.places[0].currency == p.currency for p in pending.proposed.places)
    outdoor_stops = [s for s in pending.proposed.itinerary.stops
                     if next(p.indoor for p in pending.proposed.places if p.id == s.place_id) is False]
    assert not outdoor_stops
    assert runtime.calls and "tools" not in runtime.calls[-1]
    assert all(call["max_output_tokens"] == 2000 for call in runtime.calls)


def test_refresh_clears_the_simulated_weather_override(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "plan then rain then refresh"})
    agent_app.main(agent, context)
    text = "".join(e.get("delta", "") for e in agent.events.items if e["type"] == "response.output_text.delta")
    # The override line appears only in the rain step's summary: not before the scenario, not after the refresh.
    assert text.count("Weather: simulated scenario override active (") == 1
    assert "Simulated event, not an observed forecast update." in text
    assert "Step 3 of 3: weather_updated" in text
    record = context.state["travel"]
    committed = TripSnapshot.model_validate_json(record["snapshot"])
    pending = Proposal.model_validate_json(record["proposal"])
    assert committed.revision == 2 and committed.weather_override is not None
    assert pending.trigger == "weather_updated" and pending.proposed.weather_override is None
    assert not pending.proposed.weather.source.id.startswith("scenario-")


def test_summary_reports_an_active_weather_override(make_plan, baseline):
    proposal, _, budget = make_plan(baseline, TripEvent(id="rain-1", kind="rain", simulated=True))
    lines = agent_app.summary(proposal, budget).splitlines()
    assert "Weather: simulated scenario override active (Simulated event, not an observed forecast update.)" in lines
    assert lines[-1].startswith("Type approve")
    plain, _, budget = make_plan()
    assert "Weather: simulated scenario override active" not in agent_app.summary(plain, budget)


def test_failed_step_keeps_the_steps_before_it(runtime):
    runtime.fail_when = lambda context: context["request"]["budget_minor"] == 3000
    agent, context = FakeAgent(), make_context(**{"agent.input": "plan then budget 30 then rain"})
    with pytest.raises(RuntimeError, match="simulated model outage"):
        agent_app.main(agent, context)
    text = "".join(e.get("delta", "") for e in agent.events.items if e["type"] == "response.output_text.delta")
    assert "Committed revision 1 to continue the scenario." in text and "Step 2 of 3: budget_changed" in text
    record = context.state["travel"]
    committed = TripSnapshot.model_validate_json(record["snapshot"])
    assert committed.revision == 1 and committed.request.budget_minor == 6000 and committed.itinerary.stops
    assert record["proposal"] == ""  # the failed step left no half-built proposal behind


def test_session_auto_model_call_cap_and_output_tokens(runtime):
    assert agent_app.Session(FakeAgent(), make_context()).max_model_calls == 10
    assert agent_app.Session(FakeAgent(), make_context(**{"agent.max-tool-turns": 1})).max_model_calls == 15
    explicit = agent_app.Session(FakeAgent(), make_context(**{"agent.max-model-calls": 7, "agent.max-output-tokens": 900}))
    assert explicit.max_model_calls == 7 and explicit.max_output_tokens == 900
    budget = explicit.budget()
    assert budget.max_model_calls == 7 and budget.wall_time_s == 120 and budget.model_calls == 0
    assert agent_app.Session(FakeAgent(), make_context()).max_output_tokens == 2000


def test_public_web_requires_tool_turns(runtime):
    with pytest.raises(ValueError, match=r"agent.public-web requires agent.max-tool-turns >= 1"):
        agent_app.Session(FakeAgent(), make_context(**{"agent.public-web": True}))
    session = agent_app.Session(FakeAgent(), make_context(**{"agent.public-web": True, "agent.max-tool-turns": 1}))
    assert session.max_tool_turns == 1


def test_prose_wrapped_model_output_still_plans(runtime):
    runtime.prose = True
    agent, context = FakeAgent(), make_context()
    agent_app.main(agent, context)
    assert not any(e["type"] == "travel.model.repair" for e in agent.events.items)
    assert Proposal.model_validate_json(context.state["travel"]["proposal"]).proposed.itinerary.validation.valid


def test_job_mode_returns_proposal_as_run_event(runtime, baseline):
    snapshot = baseline.model_copy(deep=True)
    snapshot.agent_mode = "model"
    blob = agent_app.jobcodec.encode({"snapshot": snapshot.model_dump(mode="json", exclude={"reports", "messages"}),
                                      "event": {"id": "rain-1", "kind": "rain", "payload": {}, "simulated": True}})
    agent, context = FakeAgent(), make_context(**{"agent.input": json.dumps({"job": blob})})
    agent_app.main(agent, context)
    result = next(e for e in agent.events.items if e["type"] == "travel.result")
    proposal = Proposal.model_validate(jobcodec.decode(result["proposal"])["proposal"])
    assert proposal.trip_id == snapshot.id and proposal.base_revision == snapshot.revision and proposal.trigger == "rain"
    assert proposal.proposed.revision == snapshot.revision + 1 and result["metrics"]["model_calls"] >= 4
    assert "travel" not in context.state


def test_job_mode_reports_failures_as_events(runtime, baseline):
    blob = jobcodec.encode({"snapshot": baseline.model_dump(mode="json"), "event": {"id": "x", "kind": "place_unavailable",
                                                                                     "payload": {"place_id": "nope"}}})
    agent, context = FakeAgent(), make_context(**{"agent.input": json.dumps({"job": blob})})
    with pytest.raises(ValueError):
        agent_app.main(agent, context)
    failure = next(e for e in agent.events.items if e["type"] == "travel.failed")
    assert failure["error"].startswith("ValueError") and "test-key" not in failure["error"]


def test_unknown_command_shows_help(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "book a hotel"})
    agent_app.main(agent, context)
    assert agent.events.items[0]["delta"].startswith("Start with")


def test_jobcodec_roundtrip_and_tamper_detection():
    blob = jobcodec.encode({"snapshot": {"id": "a" * 32}, "event": None})
    assert jobcodec.decode(blob) == {"snapshot": {"id": "a" * 32}, "event": None}
    with pytest.raises(ValueError):
        jobcodec.decode(blob[:-6] + "AAAAAA")
    with pytest.raises(ValueError):
        jobcodec.decode("not-a-job")


def test_jobcodec_bounds_decompressed_size(monkeypatch):
    monkeypatch.setattr(jobcodec, "MAX_DECODED_BYTES", 1000)
    with pytest.raises(ValueError, match="size limit"):
        jobcodec.decode(jobcodec.encode({"big": "x" * 5000}))


def test_flower_bundle_configuration():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    flwr = config["tool"]["flwr"]["app"]
    assert flwr["components"]["agentapp"] == "travel_agent.agent_app:app"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", flwr["publisher"]) and flwr["publisher"] != "local"
    assert {"LICENSE", "README.md", "travel_agent/fixtures/*.json", "travel_agent/**/*.py"} <= set(flwr["fab-include"])
    agent = flwr["config"]["agent"]
    assert agent["model"] == agent_app.DEFAULT_MODEL == "flower-endeavor-v1.0"
    assert {"input", "model", "max-tool-turns", "model-timeout-s", "wall-time-s", "public-web", "reasoning-effort",
            "max-model-calls", "max-output-tokens"} <= set(agent)
    assert agent["max-model-calls"] == 0 and agent["max-output-tokens"] == 2000  # 0 = auto: 5 * (max-tool-turns + 2)
    assert not (agent["public-web"] and agent["max-tool-turns"] == 0)
    assert (ROOT / "LICENSE").exists() and (ROOT / "README.md").exists()
    assert any(dep.startswith("flwr>=") for dep in config["project"]["dependencies"])
    assert config["project"]["license"] == {"file": "LICENSE"}


def test_specialist_context_is_local_time_and_includes_compact_matrix(baseline):
    from travel_agent.agents import snapshot_context
    from travel_agent.planning.engine import points_for
    from travel_agent.providers.services import FixtureProvider
    matrix = FixtureProvider().matrix(points_for(baseline), "walking")
    context = snapshot_context(baseline, matrix)
    assert context["timezone"] == "Europe/Berlin"
    assert context["request"]["start"].endswith("+0200") and context["proposed_itinerary"]["stops"][0]["start"].endswith("+0200")
    assert context["weather"]["intervals"][0]["start"].startswith("2026-09-16T10:00")
    table = context["route_matrix"]
    assert table["ids"][0] == "origin" and table["ids"][-1] == "destination"
    assert len(table["duration_min"]) == len(table["ids"]) and table["duration_min"][0][0] == 0
    assert "opening_intervals" not in context["places"][0] and "legs" not in context["proposed_itinerary"]
    assert len(json.dumps(context)) < 16000  # about 4k tokens including the route table
