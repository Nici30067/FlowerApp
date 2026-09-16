"""AgentApp entry point, natural-language intake, model output tolerance, job transport and bundle configuration."""
import json
import re
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from travel_agent import agent_app, jobcodec
from travel_agent.agents import ExecutionBudget, ModelRunner, ToolDispatcher, extract_json_object
from travel_agent.providers.services import FIXTURE_CENTER, FixtureProvider, ProviderError
from travel_agent.schemas import Coordinate, Evidence, GeoPlace, Proposal, TripBrief, TripEvent, TripSnapshot
from travel_agent.settings import PROVIDER_RUN_CONFIG_KEYS, ProviderSettings

ROOT = Path(__file__).resolve().parents[1]
KYOTO = {"name": "Kyoto", "lat": 35.02107, "lon": 135.75385, "timezone": "Asia/Tokyo", "country": "Japan"}
PARIS = {"name": "Paris", "lat": 48.85341, "lon": 2.3488, "timezone": "Europe/Paris", "country": "France"}


def tomorrow() -> str:
    """The intake resolves 'tomorrow' on the local clock, as these tests do."""
    return (date.today() + timedelta(days=1)).isoformat()


def geoplace(*, name, lat, lon, timezone, country=""):
    """A GeoPlace as the Geocoder builds it from one Open-Meteo record."""
    return GeoPlace(query=name, name=name, country=country, coordinate=Coordinate(lat=lat, lon=lon), timezone=timezone,
                    source=Evidence(id="geocode-" + name.lower(), provider="Open-Meteo geocoding", status="live",
                                    retrieved_at=datetime.now(UTC)))


class FakeGeocoder:
    """Answers every query with one fixed result (or raises one error) and records the queries it received."""
    def __init__(self, result=None, error=None):
        self.result, self.error, self.queries = result, error, []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


class RelocatedFixture(FixtureProvider):
    """The Berlin fixture catalog shifted onto the request origin: a network-free stand-in for the live provider."""
    mode = "live"

    def search(self, request):
        base = request.model_copy(update={"origin": FIXTURE_CENTER, "destination": FIXTURE_CENTER})
        dlat, dlon = request.origin.lat - FIXTURE_CENTER.lat, request.origin.lon - FIXTURE_CENTER.lon
        return [p.model_copy(update={"coordinate": Coordinate(lat=p.coordinate.lat + dlat, lon=p.coordinate.lon + dlon)})
                for p in super().search(base)]


def text_of(agent) -> str:
    return "".join(e.get("delta", "") for e in agent.events.items if e["type"] == "response.output_text.delta")


def stored(context):
    """The persisted snapshot, proposal and brief of a context (None where the record holds "")."""
    record = context.state["travel"]
    return (TripSnapshot.model_validate_json(record["snapshot"]) if record["snapshot"] else None,
            Proposal.model_validate_json(record["proposal"]) if record["proposal"] else None,
            TripBrief.model_validate_json(record["brief"]) if record["brief"] else None)


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


def test_text_that_is_no_command_is_read_as_a_description(runtime):
    # Free text no longer shows HELP: the intake asks for the first missing field and points to help.
    agent, context = FakeAgent(), make_context(**{"agent.input": "book a hotel"})
    agent_app.main(agent, context)
    text = text_of(agent)
    assert "Which city are you visiting?" in text and "Send help for the command list." in text
    assert runtime.calls == [] and stored(context)[0] is None
    assert agent.events.items[-1]["type"] == "response.completed"
    agent, context = FakeAgent(), make_context(**{"agent.input": "help"})
    agent_app.main(agent, context)
    help_text = agent.events.items[0]["delta"]
    assert help_text == agent_app.HELP and help_text.startswith("Start with a description: Plan a day in Tokyo")
    assert "rain; refresh; budget 30; walk 2 km; status; approve; reject; export" in help_text


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
    travel = flwr["config"]["travel"]
    # Live OpenStreetMap data by default; every provider knob is a run-config key because a run has no environment.
    assert travel["data-mode"] == "live" and travel["router"] == "osrm" and travel["allow-public-overpass"] is True
    assert travel["contact"] == "osm-travel-companion Flower Hub AgentApp"
    assert {key.removeprefix("travel.") for key in PROVIDER_RUN_CONFIG_KEYS.values()} <= set(travel)
    assert all(travel[key] == "" for key in ("osrm-url-template", "overpass-url", "ors-url", "ors-api-key",
                                              "weather-url", "geocoder-url", "api-url", "job-id", "job-token"))
    settings = ProviderSettings.from_run_config({"travel." + key: value for key, value in travel.items()})
    assert settings.live and settings.router == "osrm" and settings.allow_public_overpass and settings.contact
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


def test_description_plans_a_brief_derived_request_in_fixture_mode(runtime):
    prompt = "Plan a day in Berlin tomorrow 10:00 to 17:00, art and coffee, budget 60"
    agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
    agent_app.main(agent, context)
    committed, pending, brief = stored(context)
    request = committed.request
    assert request.city == "Berlin" and request.title == "A day in Berlin" and request.timezone == "Europe/Berlin"
    assert request.interests == ["art", "coffee"] and request.budget_minor == 6000
    assert request.start.isoformat().startswith(tomorrow() + "T10:00:00") and request.end.isoformat().startswith(tomorrow() + "T17:00:00")
    assert (request.origin.lat, request.origin.lon) == (52.5225, 13.4024)  # the fixture city needs no geocoding
    assert committed.revision == 0 and committed.data_mode == "fixture" and committed.agent_mode == "model"
    assert pending.base_revision == 0 and pending.trigger == "initial_plan" and pending.proposed.itinerary.stops
    assert brief is None  # the brief is cleared once it has been planned
    text = text_of(agent)
    assert f"Planning A day in Berlin: {tomorrow()} 10:00 to 17:00 (Europe/Berlin); interests art, coffee; budget 60.00 EUR" in text
    assert "Step 1 of 1: initial plan" in text and "Live data" not in text and "Located" not in text
    assert runtime.calls and agent.events.items[-1]["type"] == "response.completed"
    assert all(json.loads(c["input"][0]["content"])["request"]["budget_minor"] == 6000 for c in runtime.calls)


def test_missing_time_window_gets_the_default_day(runtime):
    prompt = "Plan two days in Berlin tomorrow, art and coffee, budget 60"
    agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
    agent_app.main(agent, context)
    committed, pending, _ = stored(context)
    assert committed.request.start.isoformat().startswith(tomorrow() + "T10:00:00")
    assert committed.request.end.isoformat().startswith(tomorrow() + "T17:00:00")
    text = text_of(agent)
    assert "No time window given: planning 10:00 to 17:00 local time" in text
    assert "I will plan one day of your 2-day trip" in text and pending is not None


def test_incomplete_description_asks_and_keeps_the_brief_across_runs(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "Plan a day trip"})
    agent_app.main(agent, context)
    text = text_of(agent)
    assert "Which city are you visiting? This server plans the bundled Berlin scenario." in text
    assert "Still needed: city, date, time window, interests." in text and "So far" not in text
    assert runtime.calls == [] and stored(context) == (None, None, None)  # an empty brief is stored as ""
    # A partial answer is kept between runs of the series.
    context.run_config["agent.input"] = "Plan a day trip with art and coffee"
    agent_app.main(FakeAgent(), context)
    assert stored(context)[2] == TripBrief(interests=["art", "coffee"]) and runtime.calls == []
    context.run_config["agent.input"] = "status"
    agent = FakeAgent()
    agent_app.main(agent, context)
    assert "Conversation brief: interests art, coffee. Data mode: fixture (osrm)." in text_of(agent)
    # The next run completes the brief and plans it; the interests come from the stored brief.
    context.run_config["agent.input"] = "in Berlin tomorrow 10 to 17, budget 50"
    agent = FakeAgent()
    agent_app.main(agent, context)
    committed, pending, brief = stored(context)
    assert committed.request.city == "Berlin" and committed.request.interests == ["art", "coffee"]
    assert committed.request.budget_minor == 5000 and committed.request.start.isoformat().startswith(tomorrow() + "T10:00")
    assert pending is not None and brief is None and runtime.calls
    assert "Step 1 of 1: initial plan" in text_of(agent)


def test_live_description_geocodes_the_city_and_plans_there(runtime, monkeypatch):
    geocoder = FakeGeocoder(geoplace(**KYOTO))
    built = []
    monkeypatch.setattr(agent_app, "make_geocoder", lambda settings=None: built.append(settings) or geocoder)
    providers = []
    monkeypatch.setattr(agent_app, "make_provider",
                        lambda mode=None, settings=None: providers.append((mode, settings)) or RelocatedFixture())
    prompt = "Plan a day in Kyoto tomorrow 10 to 17, art and coffee, budget 40"
    config = {"agent.input": prompt, "travel.data-mode": "live", "travel.contact": "tests@example.org"}
    agent, context = FakeAgent(), make_context(**config)
    agent_app.main(agent, context)
    committed, pending, brief = stored(context)
    request = committed.request
    assert request.city == "Kyoto" and request.title == "A day in Kyoto" and request.timezone == "Asia/Tokyo"
    assert (request.origin.lat, request.origin.lon) == (KYOTO["lat"], KYOTO["lon"]) and request.destination == request.origin
    assert request.start.utcoffset() == timedelta(hours=9) and request.start.isoformat().startswith(tomorrow() + "T10:00:00+09:00")
    assert request.interests == ["art", "coffee"] and request.budget_minor == 4000
    assert committed.data_mode == "live" and pending.proposed.data_mode == "live" and brief is None
    assert geocoder.queries == ["Kyoto"]  # one lookup serves the check and the provenance line
    # Providers are built from the run config, never from the environment.
    assert built[0].live and built[0].contact == "tests@example.org"
    assert providers == [("live", built[0])]
    text = text_of(agent)
    assert "\nLive data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (osrm)\n" in text
    assert text.index("Live data:") < text.index("Step 1 of 1: initial plan")
    assert "Located Kyoto, Japan via Open-Meteo geocoding." in text and "Data: live." in text
    assert "start 35.0211, 135.7538" in text


def test_live_line_names_the_configured_router():
    assert agent_app.live_line(ProviderSettings(router="osrm")) == (
        "Live data: OpenStreetMap via Overpass, OSRM routes, Open-Meteo weather (osrm)")
    assert agent_app.live_line(ProviderSettings(router="ors")) == (
        "Live data: OpenStreetMap via Overpass, OpenRouteService routes, Open-Meteo weather (ors)")


def test_unsupported_city_in_fixture_mode_explains_without_model_calls(runtime, monkeypatch):
    geocoder = FakeGeocoder(geoplace(**PARIS))
    monkeypatch.setattr(agent_app, "make_geocoder", lambda settings=None: geocoder)
    agent, context = FakeAgent(), make_context(**{"agent.input": "Plan a day in Paris tomorrow 10 to 17, art"})
    agent_app.main(agent, context)
    text = text_of(agent)
    assert "Paris, France is outside the bundled Berlin scenario" in text
    assert "Re-run with --run-config 'travel.data-mode=\"live\"'" in text
    assert runtime.calls == [] and geocoder.queries == ["Paris"]
    committed, pending, brief = stored(context)
    assert committed is None and pending is None and brief.city == "Paris" and brief.interests == ["art"]
    # Naming the fixture city completes the same brief without any lookup.
    context.run_config["agent.input"] = "Berlin instead"
    agent_app.main(FakeAgent(), context)
    committed, _, brief = stored(context)
    assert committed.request.city == "Berlin" and committed.request.interests == ["art"] and brief is None
    assert geocoder.queries == ["Paris"] and runtime.calls


def test_geocoder_outage_keeps_the_brief_for_a_retry(runtime, monkeypatch):
    def down(settings=None):
        raise ProviderError("geocoding-api.open-meteo.com request failed (ConnectError)")
    monkeypatch.setattr(agent_app, "make_geocoder", down)
    # Fixture mode: the note offers Berlin, which needs no lookup.
    agent, context = FakeAgent(), make_context(**{"agent.input": "Plan a day in Paris tomorrow 10 to 17, art"})
    agent_app.main(agent, context)
    text = text_of(agent)
    assert "I couldn't look up 'Paris' right now" in text and "say Berlin, which needs no lookup" in text
    assert stored(context)[2].city == "Paris" and runtime.calls == []
    # Live mode: the same brief stays for the retry.
    config = {"agent.input": "Plan a day in Kyoto tomorrow 10 to 17, art", "travel.data-mode": "live", "travel.contact": "t"}
    agent, context = FakeAgent(), make_context(**config)
    agent_app.main(agent, context)
    assert "lookup is unavailable" in text_of(agent) and stored(context)[2].city == "Kyoto" and runtime.calls == []


def test_unknown_place_is_dropped_so_the_next_answer_names_the_city(runtime, monkeypatch):
    geocoder = FakeGeocoder(None)
    monkeypatch.setattr(agent_app, "make_geocoder", lambda settings=None: geocoder)
    monkeypatch.setattr(agent_app, "make_provider", lambda mode=None, settings=None: RelocatedFixture())
    config = {"agent.input": "Plan a day in Atlantis tomorrow 10 to 17, art", "travel.data-mode": "live", "travel.contact": "t"}
    agent, context = FakeAgent(), make_context(**config)
    agent_app.main(agent, context)
    assert "I couldn't find a place called 'Atlantis'" in text_of(agent) and runtime.calls == []
    brief = stored(context)[2]
    assert brief.city is None and brief.interests == ["art"] and brief.start_time == "10:00"
    geocoder.result = geoplace(**PARIS)
    context.run_config["agent.input"] = "paris"  # a bare lower-case answer counts because no city is known
    agent_app.main(FakeAgent(), context)
    committed, _, brief = stored(context)
    assert committed.request.city == "Paris" and committed.request.timezone == "Europe/Paris" and brief is None
    assert geocoder.queries == ["Atlantis", "Paris"]


def test_follow_up_commands_work_after_a_described_plan(runtime):
    prompt = "Plan a day in Berlin tomorrow 10 to 17, art and parks, budget 60"
    agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
    agent_app.main(agent, context)
    calls = len(runtime.calls)
    context.run_config["agent.input"] = "rain"
    agent = FakeAgent()
    agent_app.main(agent, context)
    committed, pending, brief = stored(context)
    assert committed.revision == 0 and pending.base_revision == 0 and pending.trigger == "rain"
    assert pending.proposed.weather_override is not None and pending.proposed.request.interests == ["art", "parks"]
    assert pending.proposed.request.start.isoformat().startswith(tomorrow() + "T10:00") and brief is None
    assert len(runtime.calls) > calls and "Step 1 of 1: rain (simulated scenario)" in text_of(agent)
    outdoor = [s for s in pending.proposed.itinerary.stops
               if next(p.indoor for p in pending.proposed.places if p.id == s.place_id) is False]
    assert not outdoor
    for command, check in (("approve", lambda c, p: c.revision == 1 and p is None),
                           ("budget 30", lambda c, p: c.revision == 1 and p.proposed.request.budget_minor == 3000),
                           ("walk 2 km", lambda c, p: p.proposed.request.max_walking_m == 2000 and p.base_revision == 1),
                           ("refresh", lambda c, p: p.trigger == "weather_updated" and p.proposed.weather_override is None)):
        context.run_config["agent.input"] = command
        agent_app.main(FakeAgent(), context)
        committed, pending, brief = stored(context)
        assert check(committed, pending) and brief is None, command
    context.run_config["agent.input"] = "export"
    agent = FakeAgent()
    agent_app.main(agent, context)
    assert json.loads(text_of(agent).strip("`json\n"))["revision"] == 1


def test_description_can_open_a_scripted_scenario(runtime):
    prompt = "Plan a day in Berlin tomorrow 10 to 17, art and coffee, budget 60 then rain"
    agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
    agent_app.main(agent, context)
    text = text_of(agent)
    assert "Step 1 of 2: initial plan" in text and "Committed revision 1 to continue the scenario." in text
    assert "Step 2 of 2: rain (simulated scenario)" in text
    committed, pending, brief = stored(context)
    assert committed.revision == 1 and committed.request.budget_minor == 6000 and committed.request.interests == ["art", "coffee"]
    assert pending.trigger == "rain" and pending.base_revision == 1 and brief is None
    # A later segment that is no command belongs to the description itself.
    context = make_context(**{"agent.input": "Plan a day in Berlin tomorrow; 10 to 17; art and coffee"})
    agent_app.main(FakeAgent(), context)
    committed, pending, _ = stored(context)
    assert committed.request.interests == ["art", "coffee"] and committed.request.start.isoformat().startswith(tomorrow() + "T10:00")
    assert pending.trigger == "initial_plan" and committed.revision == 0
    # Scripted steps after an incomplete description are reported, not run.
    agent, context = FakeAgent(), make_context(**{"agent.input": "Plan a day trip then rain"})
    agent_app.main(agent, context)
    assert "The 1 scripted step(s) after the description were not run." in text_of(agent)
    with pytest.raises(ValueError, match="at most 6 steps"):
        agent_app.main(FakeAgent(), make_context(**{"agent.input": "Plan a day in Berlin " + " then rain" * 6}))


def test_state_record_carries_the_brief_through_review_commands(runtime):
    agent, context = FakeAgent(), make_context(**{"agent.input": "plan"})
    agent_app.main(agent, context)
    context.run_config["agent.input"] = "Plan a day trip with parks"
    agent_app.main(FakeAgent(), context)
    committed, pending, brief = stored(context)
    assert committed is not None and pending is not None and brief == TripBrief(interests=["parks"])
    for command in ("reject", "approve"):
        context.run_config["agent.input"] = command
        if command == "approve":
            context.run_config["agent.input"] = "plan"
            agent_app.main(FakeAgent(), context)
            context.run_config["agent.input"] = "approve"
        agent_app.main(FakeAgent(), context)
        assert stored(context)[2] == TripBrief(interests=["parks"]), command
    assert stored(context)[0].revision == 1 and set(context.state["travel"].keys()) == {"snapshot", "proposal", "brief"}


def test_session_builds_providers_from_the_run_config(runtime, monkeypatch, baseline):
    seen = []
    monkeypatch.setattr(agent_app, "make_provider",
                        lambda mode=None, settings=None: seen.append((mode, settings)) or FixtureProvider())
    session = agent_app.Session(FakeAgent(), make_context(**{"travel.data-mode": "live", "travel.contact": "c",
                                                             "travel.router": "ors", "travel.ors-api-key": "k"}))
    assert session.settings.live and session.settings.router == "ors" and session.fresh(baseline.request).data_mode == "live"
    session.plan(baseline, None)  # a job snapshot keeps its own data mode; the endpoints come from the run config
    assert seen == [("fixture", session.settings)]


def test_provider_failure_names_the_service_and_keeps_the_brief_for_a_retry(runtime, monkeypatch):
    class Overloaded(FixtureProvider):
        def search(self, request):
            raise ProviderError("overpass-api.de request failed (504)", status=504)

    providers = [Overloaded(), FixtureProvider()]
    monkeypatch.setattr(agent_app, "make_provider", lambda mode=None, settings=None: providers.pop(0))
    prompt = "Plan a day in Berlin tomorrow 10 to 17, art and coffee, budget 60"
    agent, context = FakeAgent(), make_context(**{"agent.input": prompt})
    with pytest.raises(ProviderError, match="504"):
        agent_app.main(agent, context)
    text = text_of(agent)
    assert "Data provider failure: overpass-api.de request failed (504)." in text
    assert 'travel.data-mode="fixture"' in text and runtime.calls == []
    committed, pending, brief = stored(context)
    assert committed is None and pending is None
    assert brief.city == "Berlin" and brief.interests == ["art", "coffee"] and brief.budget_minor == 6000
    # Any message of the series re-plans the stored brief once the service answers again.
    context.run_config["agent.input"] = "try again please"
    agent_app.main(FakeAgent(), context)
    committed, pending, brief = stored(context)
    assert committed.request.budget_minor == 6000 and committed.request.interests == ["art", "coffee"]
    assert pending is not None and brief is None and providers == []


def test_invalid_travel_run_config_is_reported_before_any_state_or_model_call(runtime):
    config = {"agent.input": "Plan a day in Berlin tomorrow 10 to 17, art", "travel.allow-public-overpass": "maybe"}
    agent, context = FakeAgent(), make_context(**config)
    agent_app.main(agent, context)
    text = text_of(agent)
    assert text.startswith("Run-config error:") and "true or false" in text and "travel.allow-public-overpass" in text
    assert runtime.calls == [] and context.state == {}
    assert agent.events.items[-1]["type"] == "response.completed"
    assert agent_app.UNRESOLVED_MARKER == "couldn't find a place called"
