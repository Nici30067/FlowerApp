"""Offline tests for the optional model enrichment of the intake: a fake Responses client stands in for the
model, and the API falls back to the rules parser whenever the model fails."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from travel_agent import api as api_module
from travel_agent.api import create_app
from travel_agent.intake import MODEL_TIMEOUT_S, enrich_with_model
from travel_agent.schemas import IntakeMessage, TripBrief

PARIS = {"name": "Paris", "lat": 48.85341, "lon": 2.3488, "timezone": "Europe/Paris", "country": "France"}


class FakeResponses:
    """Duck-types `responses.create` of an OpenAI client or the Chat Completions adapter."""
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


def model_app(tmp_path, client, geocoder):
    return create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="model",
                      intake_client=client, geocoder=geocoder)


def test_enrich_with_model_returns_populated_brief_and_sends_a_bounded_request():
    text = brief_json(city="Berlin", date="2027-01-05", interests=["art", "history"])
    responses = FakeResponses(output_text=text)
    history = [IntakeMessage(role="assistant", text="Which city?")]
    result = enrich_with_model("art and history in Berlin on jan 5 2027", TripBrief(), history,
                               SimpleNamespace(responses=responses), "test-model")
    assert result is not None
    assert result.city == "Berlin" and result.date == "2027-01-05" and result.interests == ["art", "history"]
    call = responses.calls[0]
    assert call["model"] == "test-model" and call["timeout"] == MODEL_TIMEOUT_S == 20 and call["max_output_tokens"] == 400
    assert "JSON" in call["instructions"] and "days" not in json.dumps(call["instructions"]).lower().split("no field")[0]
    payload = json.loads(call["input"][0]["content"])
    assert call["input"][0]["role"] == "user"
    assert payload["new_message"].startswith("art and history") and payload["history"] == [
        {"role": "assistant", "text": "Which city?"}]
    assert payload["current_brief"]["city"] is None and "days" not in payload["current_brief"]


def test_enrich_with_model_returns_none_on_client_exception():
    client = SimpleNamespace(responses=FakeResponses(raise_exc=RuntimeError("boom")))
    assert enrich_with_model("hello", TripBrief(), [], client, "test-model") is None


def test_enrich_with_model_returns_none_on_non_json_text():
    client = SimpleNamespace(responses=FakeResponses(output_text="sorry, I cannot help with that"))
    assert enrich_with_model("hello", TripBrief(), [], client, "test-model") is None


def test_enrich_with_model_tolerates_prose_and_code_fences_and_maps_interest_words():
    text = 'Here you go:\n```json\n{"city": "Berlin", "interests": ["museums", "Parks", "skydiving"]}\n```'
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    result = enrich_with_model("museums and parks in Berlin", TripBrief(), [], client, "test-model")
    assert result is not None and result.city == "Berlin" and result.interests == ["art", "parks"]


def test_enrich_with_model_returns_none_on_invalid_field_value():
    # transport_mode only accepts "walking" or "cycling": an invalid literal must be rejected.
    client = SimpleNamespace(responses=FakeResponses(output_text=brief_json(transport_mode="teleport")))
    assert enrich_with_model("beam me there", TripBrief(), [], client, "test-model") is None


def test_enrich_with_model_returns_none_on_hallucinated_extra_field():
    client = SimpleNamespace(responses=FakeResponses(output_text=brief_json(city="Berlin", flight_number="LH123")))
    assert enrich_with_model("book me a flight", TripBrief(), [], client, "test-model") is None


def test_enrich_with_model_ignores_nulls_and_keeps_known_values():
    text = brief_json(city=None, start_time="10:00", end_time="", days=None)
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    result = enrich_with_model("from 10", TripBrief(city="Berlin", date="2027-01-05"), [], client, "test-model")
    assert result is not None
    assert result.city == "Berlin" and result.date == "2027-01-05" and result.start_time == "10:00"
    assert result.end_time is None


def test_intake_api_uses_model_engine_when_client_succeeds(tmp_path, fake_geocoder):
    text = brief_json(city="Berlin", date="2027-01-05", start_time="10:00", end_time="18:00",
                      interests=["art", "history"])
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    with TestClient(model_app(tmp_path, client, fake_geocoder())) as c:
        response = c.post("/api/intake", json={"message": "surprise me with something nice", "brief": {}})
    assert response.status_code == 200
    data = response.json()
    assert data["engine"] == "model" and data["ready"] is True
    assert data["request"]["end"] == "2027-01-05T18:00:00+01:00"


def test_intake_api_falls_back_to_rules_when_model_raises(tmp_path, fake_geocoder):
    client = SimpleNamespace(responses=FakeResponses(raise_exc=RuntimeError("timeout")))
    with TestClient(model_app(tmp_path, client, fake_geocoder())) as c:
        response = c.post("/api/intake", json={"message": "Berlin on 2027-01-05 for 3 days", "brief": {}})
    assert response.status_code == 200
    data = response.json()
    assert data["engine"] == "rules" and data["brief"]["city"] == "Berlin" and data["brief"]["date"] == "2027-01-05"


def test_intake_api_reports_rules_when_the_model_adds_nothing(tmp_path, fake_geocoder):
    client = SimpleNamespace(responses=FakeResponses(output_text=brief_json(city="Berlin")))
    with TestClient(model_app(tmp_path, client, fake_geocoder())) as c:
        data = c.post("/api/intake", json={"message": "Berlin", "brief": {}}).json()
    assert data["engine"] == "rules" and data["brief"]["city"] == "Berlin"


def test_model_cannot_make_an_unsupported_city_ready(tmp_path, geo, fake_geocoder):
    text = brief_json(city="Paris", date="2027-01-05", start_time="10:00", end_time="18:00", interests=["art"])
    client = SimpleNamespace(responses=FakeResponses(output_text=text))
    with TestClient(model_app(tmp_path, client, fake_geocoder(geo(**PARIS)))) as c:
        data = c.post("/api/intake", json={"message": "a nice day out", "brief": {}}).json()
    assert data["engine"] == "model" and data["ready"] is False and data["request"] is None
    assert any("serve_live.sh" in note for note in data["notes"]) and "Berlin" in data["reply"]


def test_intake_client_is_built_once_from_the_local_model_settings(tmp_path, monkeypatch, fake_geocoder):
    monkeypatch.setenv("TRAVEL_MODEL_BASE_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("TRAVEL_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TRAVEL_MODEL_API", "chat")
    monkeypatch.setenv("TRAVEL_MODEL", "gemini-test")
    responses, built = FakeResponses(output_text=brief_json(city="Berlin")), []

    def fake_openai_client(base_url, api_key, timeout_s=120, api="responses"):
        built.append((base_url, api_key, timeout_s, api))
        return SimpleNamespace(responses=responses)

    monkeypatch.setattr(api_module, "openai_client", fake_openai_client)
    app = create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="model", geocoder=fake_geocoder())
    with TestClient(app) as c:
        first = c.post("/api/intake", json={"message": "somewhere nice", "brief": {}}).json()
        second = c.post("/api/intake", json={"message": "and coffee", "brief": first["brief"]}).json()
    assert built == [("https://model.example.invalid/v1", "test-key", 20.0, "chat")]
    assert first["engine"] == "model" and first["brief"]["city"] == "Berlin"
    assert second["brief"]["interests"] == ["coffee"]
    assert [call["model"] for call in responses.calls] == ["gemini-test", "gemini-test"]
    assert all(call["timeout"] == 20.0 for call in responses.calls)


def test_a_model_client_that_cannot_be_built_leaves_the_rules_engine(tmp_path, monkeypatch, fake_geocoder):
    for name in ("TRAVEL_MODEL_BASE_URL", "TRAVEL_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="model", geocoder=fake_geocoder())
    with TestClient(app) as c:
        data = c.post("/api/intake", json={"message": "Berlin on 2027-01-05", "brief": {}}).json()
    assert data["engine"] == "rules" and data["brief"]["city"] == "Berlin"


def test_rules_mode_never_builds_a_model_client(tmp_path, monkeypatch, fake_geocoder):
    def explode(*args, **kwargs):
        raise AssertionError("openai_client must not be called in rules mode")

    monkeypatch.setattr(api_module, "openai_client", explode)
    app = create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="rules", geocoder=fake_geocoder())
    with TestClient(app) as c:
        data = c.post("/api/intake", json={"message": "Berlin", "brief": {}}).json()
    assert data["engine"] == "rules"
