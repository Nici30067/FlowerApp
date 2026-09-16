import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from travel_agent.api import create_app
from travel_agent.intake import enrich_with_model
from travel_agent.schemas import TripBrief


class FakeResponses:
    def __init__(self, output_text=None, raise_exc=None):
        self.output_text, self.raise_exc = output_text, raise_exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(output_text=self.output_text)


def brief_json(**fields):
    return json.dumps(fields)


def test_enrich_with_model_returns_populated_brief():
    text = brief_json(city="Berlin", date="2027-01-05", interests=["art", "history"])
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    result = enrich_with_model("art and history in Berlin on jan 5 2027", TripBrief(), [], client, "test-model")
    assert result is not None
    assert result.city == "Berlin"
    assert result.date == "2027-01-05"
    assert result.interests == ["art", "history"]


def test_enrich_with_model_returns_none_on_client_exception():
    client = SimpleNamespace(responses=FakeResponses(raise_exc=RuntimeError("boom")))
    result = enrich_with_model("hello", TripBrief(), [], client, "test-model")
    assert result is None


def test_enrich_with_model_returns_none_on_non_json_text():
    client = SimpleNamespace(responses=FakeResponses(output_text="sorry, I cannot help with that"))
    result = enrich_with_model("hello", TripBrief(), [], client, "test-model")
    assert result is None


def test_enrich_with_model_returns_none_on_invalid_field_value():
    # transport_mode only accepts "walking" or "cycling" — an invalid literal must be rejected.
    text = brief_json(transport_mode="teleport")
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    result = enrich_with_model("beam me there", TripBrief(), [], client, "test-model")
    assert result is None


def test_enrich_with_model_returns_none_on_hallucinated_extra_field():
    text = brief_json(city="Berlin", flight_number="LH123")
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    result = enrich_with_model("book me a flight", TripBrief(), [], client, "test-model")
    assert result is None


def test_intake_api_uses_model_engine_when_client_succeeds(tmp_path):
    text = brief_json(city="Berlin", date="2027-01-05", start_time="10:00", end_time="18:00",
                       interests=["art", "history"])
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    app = create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="model", intake_client=client)
    with TestClient(app) as c:
        response = c.post("/api/intake", json={"message": "surprise me with something nice", "brief": {}})
        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "model"
        assert data["ready"] is True


def test_intake_api_falls_back_to_rules_when_model_raises(tmp_path):
    client = SimpleNamespace(responses=FakeResponses(raise_exc=RuntimeError("timeout")))
    app = create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="model", intake_client=client)
    with TestClient(app) as c:
        response = c.post("/api/intake", json={"message": "Berlin on 5 jan for 3 days", "brief": {}})
        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "rules"
        assert data["brief"]["city"] == "Berlin"
