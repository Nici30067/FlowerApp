"""The API server drives SuperGrid runs itself in control mode; SuperGrid results are re-validated locally."""
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from travel_agent import flower_backend
from travel_agent.agents import ExecutionBudget, RulesRunner
from travel_agent.api import create_app
from travel_agent.coordinator import Coordinator
from travel_agent.flower_backend import FlowerBackend, FlowerError, decode_result, encode_job, run_id_from
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import TripEvent, TripRequest, TripSnapshot
from travel_agent.settings import ModelSettings, effective_model_calls, model_settings_from_env

INITIAL = ("completed", "failed")  # an initial plan passes through awaiting_review before accept() completes it
REVISION = ("awaiting_review", "failed")
MODEL_ENV = ("TRAVEL_MODEL", "TRAVEL_MODEL_API", "TRAVEL_MAX_TOOL_TURNS", "TRAVEL_REASONING_EFFORT", "TRAVEL_MODEL_TIMEOUT_S",
             "TRAVEL_WALL_TIME_S", "TRAVEL_MAX_MODEL_CALLS", "TRAVEL_MAX_OUTPUT_TOKENS", "TRAVEL_FLOWER_RUN_TIMEOUT_S",
             "TRAVEL_FLOWER_FEDERATION", "TRAVEL_FLOWER_CONNECTION")


def task_event(event_id, kind, payload):
    """One StreamRunEvents response with the fields the backend reads (no flwr import needed)."""
    return SimpleNamespace(task_event=SimpleNamespace(id=event_id, event=kind, data=json.dumps({"type": kind, **payload})))


def raising_once(listen, kind):
    """A listener that fails exactly once, for one event kind, like a transient store error would."""
    raised = []

    def wrapper(k, data):
        if k == kind and not raised:
            raised.append(k)
            raise RuntimeError("simulated listener failure")
        listen(k, data)
    return wrapper


class FakeSuperGrid:
    """Stands in for SuperGrid: plans with the deterministic runner, then replays the run events through the real
    FlowerBackend relay loop from an in-memory stream that can drop the connection once."""
    model = "flower-endeavor-v1.0"
    settings = ModelSettings(max_tool_turns=1)

    def __init__(self, fail=False, tamper=False, listener_raises_on=None, drop_after=None):
        self.fail, self.tamper = fail, tamper
        self.listener_raises_on, self.drop_after = listener_raises_on, drop_after
        self.jobs, self.outcomes, self.stream_opens, self.event_counts = [], [], [], []

    def describe(self):
        return {"connection": "supergrid", "federation": "@tester/workspace", "model": self.model, "max_tool_turns": 1}

    def stream(self, events, after_id):
        self.stream_opens.append(after_id)
        for item in events[(after_id or 0):]:  # task event IDs are 1-based
            if item.task_event.id == self.drop_after and len(self.stream_opens) == 1:
                raise ConnectionError("simulated transport failure")
            yield item

    def run_job(self, snapshot, event, listen):
        self.jobs.append(encode_job(snapshot, event))
        listen("flower.submitting", {"fab_hash": "deadbeef", "model": self.model, "federation": "@tester/workspace"})
        listen("flower.run.started", {"run_id": "42", "federation": "@tester/workspace", "model": self.model, "note": ""})
        if self.fail:
            raise FlowerError("SuperGrid run 42 failed: RuntimeError: Model call budget exhausted")
        events = []

        def record(kind, data):
            events.append(task_event(len(events) + 1, "travel." + kind, {"kind": kind, "data": data}))
        budget = ExecutionBudget()
        proposal = Coordinator(FixtureProvider(), RulesRunner(record, budget), record, budget).plan(snapshot, event)
        proposal.proposed.agent_mode = "model"  # the real AgentApp runs ModelRunner specialists
        if self.tamper:
            proposal.proposed.request.budget_minor += 1
        metrics = {"model_calls": 5, "tool_calls": 3, "provider_calls": 3, "elapsed_s": 41.2}
        events.append(task_event(len(events) + 1, "travel.result", {
            "proposal": flower_backend.jobcodec.encode({"proposal": proposal.model_dump(mode="json")}), "metrics": metrics}))
        self.event_counts.append(len(events))
        listener = raising_once(listen, self.listener_raises_on) if self.listener_raises_on else listen
        outcome = {}
        FlowerBackend.relay_stream(lambda after: self.stream(events, after), listener, outcome,
                                   time.monotonic() + 60, threading.Event())
        self.outcomes.append(outcome)
        listen("flower.run.finished", {"run_id": "42", "status": "completed", "metrics": outcome["result"]["metrics"],
                                       "relayed": outcome["relayed"], "listener_errors": outcome["listener_errors"]})
        return decode_result(outcome["result"])


def wait(client, job_id, terminal):
    """Poll until the job reaches one of `terminal`; intermediate statuses are skipped, not mistaken for the end."""
    job = None
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {terminal}: {job}")


def events_text(client, job_id):
    return client.get(f"/api/jobs/{job_id}/events", headers={"accept": "text/event-stream"}).text


@pytest.fixture
def grid(tmp_path):
    def make(**kwargs):
        fake = FakeSuperGrid(**kwargs)
        app = create_app(str(tmp_path / "grid.sqlite3"), data_mode="fixture", execution_backend="flower", flower_backend=fake)
        return app, fake
    return make


@pytest.fixture
def clean_env(monkeypatch):
    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_config_reports_supergrid_backend(grid):
    app, _ = grid()
    with TestClient(app) as client:
        config = client.get("/api/config").json()
    assert config["execution_backend"] == "flower" and config["flower_mode"] == "control"
    assert config["model"] == "flower-endeavor-v1.0" and config["agent_mode"] == "model" and config["model_api"] is None
    assert config["flower"]["federation"] == "@tester/workspace"
    assert config["model_settings"] == {"max_tool_turns": 1, "reasoning_effort": "low", "max_model_calls": 15,
                                        "max_output_tokens": 2000, "wall_time_s": 900}


def test_build_itinerary_is_one_action_and_streams_supergrid_events(grid):
    app, fake = grid()
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        job = created["job"]
        assert not job["external"] and "flower_command" not in job
        finished = wait(client, job["id"], INITIAL)
        assert finished["status"] == "completed" and finished["proposal_id"]
        text = events_text(client, job["id"])
        for kind in ("flower.submitting", "flower.run.started", "agent.started", "collaboration.message",
                     "validation.completed", "flower.run.finished", "job.finished"):
            assert f'"type": "{kind}"' in text
        assert '"model_calls": 5' in text and '"provider_calls": 3' in text
        trip = client.get(f"/api/trips/{created['trip_id']}").json()["trip"]
    assert trip["revision"] == 1 and trip["agent_mode"] == "model" and len(fake.jobs) == 1
    assert fake.outcomes[0]["listener_errors"] == 0 and fake.outcomes[0]["relayed"] == fake.event_counts[0]
    decoded = flower_backend.jobcodec.decode(fake.jobs[0])
    assert decoded["event"] is None and decoded["snapshot"]["revision"] == 0 and "reports" not in decoded["snapshot"]


def test_replanning_round_trips_through_supergrid_and_awaits_review(grid):
    app, fake = grid()
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        wait(client, created["job"]["id"], INITIAL)
        body = {"base_revision": 1, "event": {"id": "rain1", "kind": "rain", "payload": {}, "simulated": True}}
        job = client.post(f"/api/trips/{created['trip_id']}/events", json=body).json()["job"]
        finished = wait(client, job["id"], REVISION)
        assert finished["status"] == "awaiting_review"
        proposals = client.get(f"/api/trips/{created['trip_id']}").json()["proposals"]
    assert proposals and proposals[0]["trigger"] == "rain" and proposals[0]["base_revision"] == 1
    decoded = flower_backend.jobcodec.decode(fake.jobs[-1])
    assert decoded["event"]["kind"] == "rain" and decoded["snapshot"]["revision"] == 1


def test_supergrid_failure_is_reported_without_credentials(grid):
    app, _ = grid(fail=True)
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        finished = wait(client, created["job"]["id"], INITIAL)
    assert finished["status"] == "failed" and finished["error"].startswith("SuperGrid run 42 failed")


def test_tampered_supergrid_result_is_rejected(grid):
    app, _ = grid(tamper=True)
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        finished = wait(client, created["job"]["id"], INITIAL)
    assert finished["status"] == "failed" and "user constraints" in finished["error"]


def test_listener_failure_does_not_fail_the_job(grid):
    app, fake = grid(listener_raises_on="agent.completed")
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        finished = wait(client, created["job"]["id"], INITIAL)
        assert finished["status"] == "completed" and finished["proposal_id"]
        text = events_text(client, created["job"]["id"])
    assert fake.outcomes[0]["listener_errors"] == 1 and "result" in fake.outcomes[0]
    assert '"type": "agent.completed"' in text and '"listener_errors": 1' in text and '"type": "job.finished"' in text


def test_dropped_event_stream_resumes_after_the_last_relayed_event(grid, monkeypatch):
    sleeps = []
    monkeypatch.setattr(flower_backend, "_sleep", sleeps.append)
    app, fake = grid(drop_after=4)
    with TestClient(app) as client:
        created = client.post("/api/trips", json={}).json()
        finished = wait(client, created["job"]["id"], INITIAL)
        assert finished["status"] == "completed"
        text = events_text(client, created["job"]["id"])
    assert fake.stream_opens == [None, 3] and sleeps == [2]
    outcome = fake.outcomes[0]
    assert outcome["reconnects"] == 1 and outcome["relayed"] == fake.event_counts[0] and outcome["listener_errors"] == 0
    assert '"type": "flower.stream.reconnecting"' in text and '"after_task_event_id": 3' in text
    assert text.count('"type": "coordinator.started"') == 1  # nothing before the drop is replayed


def test_relay_stream_gives_up_after_three_reconnects(monkeypatch):
    sleeps, opens, seen, outcome = [], [], [], {}
    monkeypatch.setattr(flower_backend, "_sleep", sleeps.append)

    def open_stream(after_id):
        opens.append(after_id)
        yield task_event(len(opens), "travel.agent.started", {"kind": "agent.started", "data": {"role": "discovery"}})
        raise ConnectionError("gone")
    FlowerBackend.relay_stream(open_stream, lambda k, d: seen.append(k), outcome, time.monotonic() + 60, threading.Event())
    assert opens == [None, 1, 2, 3] and sleeps == [2, 4, 6]
    assert isinstance(outcome["error"], ConnectionError) and outcome["relayed"] == 4 and outcome["reconnects"] == 3
    assert seen.count("flower.stream.reconnecting") == 3 and seen.count("agent.started") == 4 and "result" not in outcome


def test_relay_stream_respects_the_deadline_and_the_stop_signal(monkeypatch):
    sleeps = []
    monkeypatch.setattr(flower_backend, "_sleep", sleeps.append)

    def open_stream(after_id):
        raise ConnectionError("gone")
    outcome = {}
    FlowerBackend.relay_stream(open_stream, lambda k, d: None, outcome, time.monotonic() + 1, threading.Event())
    assert sleeps == [] and isinstance(outcome["error"], ConnectionError) and outcome["relayed"] == 0
    stopped, outcome = threading.Event(), {}
    stopped.set()
    FlowerBackend.relay_stream(open_stream, lambda k, d: None, outcome, time.monotonic() + 60, stopped)
    assert "error" not in outcome and outcome["relayed"] == 0 and sleeps == []


def test_relay_stream_stops_at_the_agentapp_verdict():
    events = [task_event(1, "travel.agent.started", {"kind": "agent.started", "data": {"role": "discovery"}}),
              task_event(2, "travel.failed", {"error": "RuntimeError: Model call budget exhausted"}),
              task_event(3, "travel.result", {"proposal": "never read"})]
    seen, outcome = [], {}
    FlowerBackend.relay_stream(lambda after: iter(events), lambda k, d: seen.append(k), outcome,
                               time.monotonic() + 60, threading.Event())
    assert outcome["failure"].startswith("RuntimeError") and "result" not in outcome and seen == ["agent.started"]
    outcome = {}
    FlowerBackend.relay_stream(lambda after: iter([task_event(1, "response.failed", {})]), lambda k, d: None, outcome,
                               time.monotonic() + 60, threading.Event())
    assert outcome["failure"] == "Model response failed on SuperGrid"


def test_live_data_is_refused_for_supergrid_control_mode(tmp_path):
    with pytest.raises(ValueError, match="fixture"):
        create_app(str(tmp_path / "x.sqlite3"), data_mode="live", execution_backend="flower", flower_backend=FakeSuperGrid())


def test_model_settings_defaults_and_env_overrides(clean_env):
    defaults = model_settings_from_env()
    assert defaults == ModelSettings(model="flower-endeavor-v1.0", api="responses", max_tool_turns=0, reasoning_effort="low",
                                     model_timeout_s=120.0, wall_time_s=900, max_model_calls=0, max_output_tokens=2000)
    assert defaults.model_call_cap == 10 and effective_model_calls(0, 2) == 20 and effective_model_calls(7, 2) == 7
    clean_env.setenv("TRAVEL_MODEL", " gemini-3.8-flash ")
    clean_env.setenv("TRAVEL_MODEL_API", "chat")
    clean_env.setenv("TRAVEL_MAX_TOOL_TURNS", "2")
    clean_env.setenv("TRAVEL_REASONING_EFFORT", "")  # chat endpoints omit the reasoning field
    clean_env.setenv("TRAVEL_MODEL_TIMEOUT_S", "45")
    clean_env.setenv("TRAVEL_WALL_TIME_S", "600")
    clean_env.setenv("TRAVEL_MAX_MODEL_CALLS", "9")
    clean_env.setenv("TRAVEL_MAX_OUTPUT_TOKENS", "1200")
    custom = model_settings_from_env()
    assert custom == ModelSettings("gemini-3.8-flash", "chat", 2, "", 45.0, 600, 9, 1200) and custom.model_call_cap == 9
    assert custom.describe() == {"max_tool_turns": 2, "reasoning_effort": "", "max_model_calls": 9,
                                 "max_output_tokens": 1200, "wall_time_s": 600}
    clean_env.setenv("TRAVEL_MAX_MODEL_CALLS", "")  # an empty numeric variable means the default
    assert model_settings_from_env().max_model_calls == 0
    with pytest.raises(ValueError):
        ModelSettings(wall_time_s=0)
    with pytest.raises(ValueError):
        ModelSettings(max_output_tokens=0)


def test_from_env_builds_the_backend_from_model_settings(clean_env):
    backend = FlowerBackend.from_env()
    assert backend.settings == ModelSettings() and backend.model == "flower-endeavor-v1.0" and backend.federation == ""
    assert backend.connection == "supergrid" and backend.run_timeout_s == 1200
    clean_env.setenv("TRAVEL_FLOWER_FEDERATION", "@tester/workspace")
    clean_env.setenv("TRAVEL_FLOWER_CONNECTION", "staging")
    clean_env.setenv("TRAVEL_MAX_TOOL_TURNS", "1")
    clean_env.setenv("TRAVEL_WALL_TIME_S", "600")
    clean_env.setenv("TRAVEL_MAX_OUTPUT_TOKENS", "1500")
    backend = FlowerBackend.from_env()
    assert backend.federation == "@tester/workspace" and backend.connection == "staging"
    assert backend.settings == ModelSettings(max_tool_turns=1, wall_time_s=600, max_output_tokens=1500)
    assert backend.run_timeout_s == 900 and backend.max_tool_turns == 1 and backend.reasoning_effort == "low"
    described = backend.describe()
    assert described["max_model_calls"] == 15 and described["wall_time_s"] == 600 and described["run_timeout_s"] == 900
    assert described["max_output_tokens"] == 1500 and described["federation"] == "@tester/workspace"
    clean_env.setenv("TRAVEL_FLOWER_RUN_TIMEOUT_S", "2000")
    assert FlowerBackend.from_env().run_timeout_s == 2000


def test_overrides_forward_the_effective_budget():
    backend = FlowerBackend(federation="@tester/workspace", model="flower-endeavor-v1.0", max_tool_turns=1)
    overrides = backend._overrides("tcj1:x:y", "fixture")
    assert overrides["agent.model"] == "flower-endeavor-v1.0" and overrides["travel.data-mode"] == "fixture"
    assert '"job": "tcj1:x:y"' in overrides["agent.input"]
    assert overrides["agent.max-tool-turns"] == 1 and overrides["agent.max-model-calls"] == 15  # auto: 5 * (1 + 2)
    assert overrides["agent.wall-time-s"] == 900 and overrides["agent.max-output-tokens"] == 2000
    assert overrides["agent.model-timeout-s"] == 120.0 and overrides["agent.reasoning-effort"] == "low"
    explicit = FlowerBackend(settings=ModelSettings(max_model_calls=4, max_tool_turns=3, max_output_tokens=900, wall_time_s=300))
    forwarded = explicit._overrides("tcj1:x:y", "fixture")
    assert forwarded["agent.max-model-calls"] == 4 and forwarded["agent.max-output-tokens"] == 900
    assert forwarded["agent.wall-time-s"] == 300 and forwarded["agent.max-tool-turns"] == 3
    assert set(forwarded) == {"agent.input", "agent.model", "agent.max-tool-turns", "agent.model-timeout-s",
                              "agent.reasoning-effort", "agent.max-model-calls", "agent.wall-time-s",
                              "agent.max-output-tokens", "travel.data-mode"}


def test_run_timeout_defaults_and_floor():
    assert FlowerBackend().run_timeout_s == 1200  # wall time + 300
    assert FlowerBackend(wall_time_s=600).run_timeout_s == 900
    assert FlowerBackend(wall_time_s=600, run_timeout_s=30).run_timeout_s == 660  # never below wall time + 60
    assert FlowerBackend(wall_time_s=600, run_timeout_s=2000).run_timeout_s == 2000


def test_backend_configuration_validation():
    with pytest.raises(ValueError):
        FlowerBackend(model="   ")
    with pytest.raises(ValueError):
        FlowerBackend(federation="workspace")
    with pytest.raises(TypeError):
        FlowerBackend(unknown_setting=1)


class FakeStartRunResponse:
    """The proto3 optional-field surface of StartRunResponse that the backend reads."""

    def __init__(self, run_id=None, note=None):
        self.run_id, self.note = run_id or 0, note or ""
        self._set = {name for name, value in (("run_id", run_id), ("note", note)) if value is not None}

    def HasField(self, name):
        return name in self._set


def test_start_run_without_run_id_surfaces_supergrid_note():
    assert run_id_from(FakeStartRunResponse(run_id=42)) == 42
    with pytest.raises(FlowerError, match="SuperGrid did not start a run: Insufficient credits"):
        run_id_from(FakeStartRunResponse(note="Insufficient credits"))
    with pytest.raises(FlowerError, match="SuperGrid did not start a run: no reason was given"):
        run_id_from(FakeStartRunResponse())


class FakeControlClient:
    """A scripted Control API client: records StartRun calls, stream opens, StopRun calls and close()."""

    def __init__(self, stream):
        self.stream, self.starts, self.opens, self.stops, self.closed = stream, 0, [], [], False

    def StartRun(self, request):
        from flwr.proto.control_pb2 import StartRunResponse
        self.starts += 1
        return StartRunResponse(run_id=42)

    def StreamRunEvents(self, request):
        after = request.after_task_event_id if request.HasField("after_task_event_id") else None
        self.opens.append(after)
        return self.stream(after, len(self.opens))

    def StopRun(self, request):
        from flwr.proto.control_pb2 import StopRunResponse
        self.stops.append(request.run_id)
        return StopRunResponse(success=True)

    def close(self):
        self.closed = True


@pytest.fixture
def control_run(monkeypatch):
    """The real run_job against a FakeControlClient: no FAB build, no Control API connection, no sleeping."""
    sleeps = []
    monkeypatch.setattr(flower_backend, "_sleep", sleeps.append)
    events = []

    def record(kind, data):
        events.append(task_event(len(events) + 1, "travel." + kind, {"kind": kind, "data": data}))
    snapshot = TripSnapshot(id="control", request=TripRequest(), data_mode="fixture", agent_mode="model")
    budget = ExecutionBudget()
    runner = RulesRunner(record, budget)
    proposal = Coordinator(FixtureProvider(), runner, record, budget).plan(snapshot, None)
    proposal.proposed.agent_mode = "model"
    events.append(task_event(len(events) + 1, "travel.result", {
        "proposal": flower_backend.jobcodec.encode({"proposal": proposal.model_dump(mode="json")}),
        "metrics": {"model_calls": 5, "tool_calls": 3, "provider_calls": 3, "elapsed_s": 41.2}}))

    def make(stream):
        """`stream(after_task_event_id, open_number)` yields the events of one StreamRunEvents call."""
        client = FakeControlClient(stream)
        backend = FlowerBackend(federation="@tester/workspace", model="flower-endeavor-v1.0")
        backend.fab = lambda: ("0123456789abcdef", b"fake-fab")
        backend._client = lambda: client
        return backend, client
    return SimpleNamespace(snapshot=snapshot, events=events, sleeps=sleeps, make=make)


def test_run_job_resumes_a_dropped_stream_after_the_last_relayed_event(control_run):
    def drop_once(after, open_no):
        for item in control_run.events[(after or 0):]:
            if item.task_event.id == 4 and open_no == 1:
                raise ConnectionError("simulated transport failure")
            yield item
    backend, client = control_run.make(drop_once)
    seen = []
    proposal = backend.run_job(control_run.snapshot, None, lambda k, d: seen.append((k, d)))
    kinds = [k for k, _ in seen]
    assert proposal.proposed.agent_mode == "model" and client.starts == 1 and client.closed
    assert client.opens == [None, 3] and client.stops == [] and control_run.sleeps == [2]
    assert kinds.count("flower.stream.reconnecting") == 1 and kinds.count("coordinator.started") == 1
    assert next(d for k, d in seen if k == "flower.run.finished") == {
        "run_id": "42", "status": "completed", "relayed": len(control_run.events), "listener_errors": 0,
        "metrics": {"model_calls": 5, "tool_calls": 3, "provider_calls": 3, "elapsed_s": 41.2}}


def test_run_job_stops_the_run_when_the_stream_is_lost(control_run):
    def always_fails(after, open_no):
        yield task_event(open_no, "travel.agent.started", {"kind": "agent.started", "data": {"role": "discovery"}})
        raise ConnectionError("gone")
    backend, client = control_run.make(always_fails)
    kinds = []
    expected = (r"^Lost the SuperGrid event stream for run 42 after relaying 4 events \(ConnectionError\); "
                r"the run was stopped$")
    with pytest.raises(FlowerError, match=expected):
        backend.run_job(control_run.snapshot, None, lambda k, d: kinds.append(k))
    assert client.opens == [None, 1, 2, 3] and control_run.sleeps == [2, 4, 6]
    assert client.stops == [42] and client.closed and kinds.count("flower.stream.reconnecting") == 3


def test_run_job_stops_a_run_that_outlives_the_local_timeout(control_run):
    release = threading.Event()

    def hangs(after, open_no):
        yield control_run.events[0]
        release.wait(10)  # the AgentApp went silent; the local backstop must not wait for it
    backend, client = control_run.make(hangs)
    backend.run_timeout_s = 1.0  # the constructor floors this at wall_time_s + 60 for real runs
    expected = r"^SuperGrid run 42 exceeded 1s after relaying 1 events; the run was stopped$"
    try:
        with pytest.raises(FlowerError, match=expected):
            backend.run_job(control_run.snapshot, None, lambda k, d: None)
    finally:
        release.set()
    assert client.stops == [42] and client.closed and control_run.sleeps == []


def test_run_job_survives_a_store_failure_on_its_own_run_events(control_run):
    """A locked store on flower.run.started (after StartRun) must not abandon the live run (finding F13)."""
    backend, client = control_run.make(lambda after, open_no: iter(control_run.events[(after or 0):]))
    seen = []

    def locked_store(kind, data):
        seen.append((kind, data))
        if kind in ("flower.run.started", "flower.run.finished"):
            raise sqlite3.OperationalError("database is locked")
    proposal = backend.run_job(control_run.snapshot, None, locked_store)
    finished = next(d for k, d in seen if k == "flower.run.finished")
    assert proposal.proposed.agent_mode == "model" and client.starts == 1 and client.closed
    assert client.opens == [None] and client.stops == [] and finished["relayed"] == len(control_run.events)
    assert finished["listener_errors"] == 1  # the flower.run.started failure; the finished one cannot report itself
    # Before StartRun nothing is billed, so a broken store still fails the job instead of starting a run.
    backend, client = control_run.make(lambda after, open_no: iter(control_run.events))

    def broken_store(kind, data):
        raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError):
        backend.run_job(control_run.snapshot, None, broken_store)
    assert client.starts == 0 and client.opens == [] and client.stops == []


def test_local_paths_report_model_settings_and_provider_calls(tmp_path, clean_env):
    clean_env.setenv("TRAVEL_MODEL", "gemini-3.8-flash")
    clean_env.setenv("TRAVEL_MODEL_API", "chat")
    clean_env.setenv("TRAVEL_MAX_TOOL_TURNS", "1")
    clean_env.setenv("TRAVEL_MAX_OUTPUT_TOKENS", "1500")
    clean_env.setenv("TRAVEL_WALL_TIME_S", "300")
    clean_env.setenv("TRAVEL_REASONING_EFFORT", "")
    model_app = create_app(str(tmp_path / "local.sqlite3"), data_mode="fixture", agent_mode="model")
    with TestClient(model_app) as client:
        config = client.get("/api/config").json()
    assert config["execution_backend"] == "local" and config["model"] == "gemini-3.8-flash" and config["model_api"] == "chat"
    assert config["model_settings"] == {"max_tool_turns": 1, "reasoning_effort": "", "max_model_calls": 15,
                                        "max_output_tokens": 1500, "wall_time_s": 300}
    rules_app = create_app(str(tmp_path / "rules.sqlite3"), data_mode="fixture", agent_mode="rules")
    with TestClient(rules_app) as client:
        config = client.get("/api/config").json()
        assert config["model_settings"] is None and config["model"] is None and config["model_api"] is None
        created = client.post("/api/trips", json={}).json()
        assert wait(client, created["job"]["id"], INITIAL)["status"] == "completed"
        text = events_text(client, created["job"]["id"])
    assert '"type": "job.finished"' in text and '"provider_calls"' in text


def test_result_codec_round_trip(baseline):
    budget = ExecutionBudget()
    emit = lambda t, d: None
    proposal = Coordinator(FixtureProvider(), RulesRunner(emit, budget), emit, budget).plan(
        baseline, TripEvent(id="e1", kind="budget_changed", payload={"budget_minor": 3000}))
    payload = {"proposal": flower_backend.jobcodec.encode({"proposal": proposal.model_dump(mode="json")})}
    assert decode_result(payload) == proposal
