"""The API server drives SuperGrid runs itself in control mode; SuperGrid results are re-validated locally."""
import time

import pytest
from fastapi.testclient import TestClient

from travel_agent import flower_backend
from travel_agent.agents import ExecutionBudget, RulesRunner
from travel_agent.api import create_app
from travel_agent.coordinator import Coordinator
from travel_agent.flower_backend import FlowerBackend, FlowerError, decode_result, encode_job
from travel_agent.providers.services import FixtureProvider
from travel_agent.schemas import TripEvent


class FakeSuperGrid:
    """Stands in for SuperGrid: plans with the deterministic runner and relays events like a real run would."""
    model = "flower-endeavor-v1.0"

    def __init__(self, fail=False, tamper=False):
        self.fail, self.tamper, self.jobs = fail, tamper, []

    def describe(self):
        return {"connection": "supergrid", "federation": "@tester/workspace", "model": self.model, "max_tool_turns": 1}

    def run_job(self, snapshot, event, listen):
        self.jobs.append(encode_job(snapshot, event))
        listen("flower.submitting", {"fab_hash": "deadbeef", "model": self.model, "federation": "@tester/workspace"})
        listen("flower.run.started", {"run_id": "42", "federation": "@tester/workspace", "model": self.model, "note": ""})
        if self.fail:
            raise FlowerError("SuperGrid run 42 failed: RuntimeError: Model call budget exhausted")
        budget = ExecutionBudget()
        proposal = Coordinator(FixtureProvider(), RulesRunner(listen, budget), listen, budget).plan(snapshot, event)
        proposal.proposed.agent_mode = "model"  # the real AgentApp runs ModelRunner specialists
        if self.tamper:
            proposal.proposed.request.budget_minor += 1
        listen("flower.run.finished", {"run_id": "42", "status": "completed", "metrics": {"model_calls": 5, "tool_calls": 3, "elapsed_s": 41.2}})
        return proposal


def wait(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def grid(tmp_path):
    def make(**kwargs):
        fake = FakeSuperGrid(**kwargs)
        app = create_app(str(tmp_path / "grid.sqlite3"), data_mode="fixture", execution_backend="flower", flower_backend=fake)
        return TestClient(app), fake
    return make


def test_config_reports_supergrid_backend(grid):
    client, fake = grid()
    config = client.get("/api/config").json()
    assert config["execution_backend"] == "flower" and config["flower_mode"] == "control"
    assert config["model"] == "flower-endeavor-v1.0" and config["agent_mode"] == "model"
    assert config["flower"]["federation"] == "@tester/workspace"


def test_build_itinerary_is_one_action_and_streams_supergrid_events(grid):
    client, fake = grid()
    created = client.post("/api/trips", json={}).json()
    job = created["job"]
    assert not job["external"] and "flower_command" not in job
    finished = wait(client, job["id"])
    assert finished["status"] == "completed" and finished["proposal_id"]
    events = client.get(f"/api/jobs/{job['id']}/events", headers={"accept": "text/event-stream"})
    text = events.text
    for kind in ("flower.submitting", "flower.run.started", "agent.started", "collaboration.message", "validation.completed",
                 "flower.run.finished", "job.finished"):
        assert f'"type": "{kind}"' in text
    assert '"model_calls": 5' in text
    trip = client.get(f"/api/trips/{created['trip_id']}").json()["trip"]
    assert trip["revision"] == 1 and trip["agent_mode"] == "model" and len(fake.jobs) == 1
    decoded = flower_backend.jobcodec.decode(fake.jobs[0])
    assert decoded["event"] is None and decoded["snapshot"]["revision"] == 0 and "reports" not in decoded["snapshot"]


def test_replanning_round_trips_through_supergrid_and_awaits_review(grid):
    client, fake = grid()
    created = client.post("/api/trips", json={}).json()
    wait(client, created["job"]["id"])
    body = {"base_revision": 1, "event": {"id": "rain1", "kind": "rain", "payload": {}, "simulated": True}}
    job = client.post(f"/api/trips/{created['trip_id']}/events", json=body).json()["job"]
    finished = wait(client, job["id"])
    assert finished["status"] == "awaiting_review"
    proposals = client.get(f"/api/trips/{created['trip_id']}").json()["proposals"]
    assert proposals and proposals[0]["trigger"] == "rain" and proposals[0]["base_revision"] == 1
    decoded = flower_backend.jobcodec.decode(fake.jobs[-1])
    assert decoded["event"]["kind"] == "rain" and decoded["snapshot"]["revision"] == 1


def test_supergrid_failure_is_reported_without_credentials(grid):
    client, _ = grid(fail=True)
    created = client.post("/api/trips", json={}).json()
    finished = wait(client, created["job"]["id"])
    assert finished["status"] == "failed" and finished["error"].startswith("SuperGrid run 42 failed")


def test_tampered_supergrid_result_is_rejected(grid):
    client, _ = grid(tamper=True)
    created = client.post("/api/trips", json={}).json()
    finished = wait(client, created["job"]["id"])
    assert finished["status"] == "failed" and "user constraints" in finished["error"]


def test_live_data_is_refused_for_supergrid_control_mode(tmp_path):
    with pytest.raises(ValueError, match="fixture"):
        create_app(str(tmp_path / "x.sqlite3"), data_mode="live", execution_backend="flower", flower_backend=FakeSuperGrid())


def test_backend_configuration_validation():
    with pytest.raises(ValueError):
        FlowerBackend(model="   ")
    with pytest.raises(ValueError):
        FlowerBackend(federation="workspace")
    backend = FlowerBackend(federation="@tester/workspace", model="flower-endeavor-v1.0", max_tool_turns=1)
    overrides = backend._overrides("tcj1:x:y", "fixture")
    assert overrides["agent.model"] == "flower-endeavor-v1.0" and overrides["travel.data-mode"] == "fixture"
    assert '"job": "tcj1:x:y"' in overrides["agent.input"]


def test_result_codec_round_trip(baseline):
    budget = ExecutionBudget()
    emit = lambda t, d: None
    proposal = Coordinator(FixtureProvider(), RulesRunner(emit, budget), emit, budget).plan(
        baseline, TripEvent(id="e1", kind="budget_changed", payload={"budget_minor": 3000}))
    payload = {"proposal": flower_backend.jobcodec.encode({"proposal": proposal.model_dump(mode="json")})}
    assert decode_result(payload) == proposal
