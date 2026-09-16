"""Offline tests for POST /api/intake: the chat turn contract, city support through an injected geocoder, and
the /api/config keys the chat UI reads. No test touches the network."""
import pytest
from fastapi.testclient import TestClient

from travel_agent.api import create_app
from travel_agent.providers.services import ProviderError

TOKYO = {"name": "Tokyo", "lat": 35.6895, "lon": 139.69171, "timezone": "Asia/Tokyo", "country": "Japan"}
PARIS = {"name": "Paris", "lat": 48.85341, "lon": 2.3488, "timezone": "Europe/Paris", "country": "France"}
KREUZBERG = {"name": "Kreuzberg", "lat": 52.49973, "lon": 13.40338, "timezone": "Europe/Berlin", "country": "Germany"}


def app_client(tmp_path, geocoder, **options):
    options = {"data_mode": "fixture", "agent_mode": "rules", **options}
    return TestClient(create_app(str(tmp_path / "api.sqlite3"), geocoder=geocoder, **options))


def turn(c, message, brief=None):
    response = c.post("/api/intake", json={"message": message, "brief": brief or {}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def live_env(monkeypatch):
    """Live data mode without any network: the provider is only constructed, never called."""
    for name in ("ORS_API_KEY", "OSRM_URL_TEMPLATE", "TRAVEL_OVERPASS_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRAVEL_CONTACT", "test@example.invalid")
    monkeypatch.setenv("TRAVEL_ALLOW_PUBLIC_OVERPASS", "true")
    monkeypatch.setenv("TRAVEL_ROUTER", "osrm")


def test_intake_requires_auth_like_other_api_routes(tmp_path, monkeypatch, fake_geocoder):
    monkeypatch.setenv("TRAVEL_API_TOKEN", "secret")
    with app_client(tmp_path, fake_geocoder()) as c:
        assert c.post("/api/intake", json={"message": "Berlin", "brief": {}}).status_code == 401
        authorised = c.post("/api/intake", json={"message": "Berlin", "brief": {}},
                            headers={"Authorization": "Bearer secret"})
        assert authorised.status_code == 200


def test_intake_converges_to_a_ready_single_day_request(tmp_path, fake_geocoder):
    fake = fake_geocoder()
    with app_client(tmp_path, fake) as c:
        first = turn(c, "Berlin on 2027-01-05 for 3 days")
        assert first["ready"] is False and first["request"] is None and first["engine"] == "rules"
        assert first["missing"] == ["time_window", "interests"]
        assert any("one day at a time" in note for note in first["notes"])
        assert "time" in first["reply"]
        second = turn(c, "10 to 5", first["brief"])
        assert second["ready"] is False and second["missing"] == ["interests"] and second["notes"] == []
        third = turn(c, "art and history", second["brief"])
    assert third["ready"] is True and third["missing"] == [] and third["notes"] == []
    request = third["request"]
    assert request["city"] == "Berlin" and request["timezone"] == "Europe/Berlin"
    assert request["title"] == "A day in Berlin"
    assert request["start"] == "2027-01-05T10:00:00+01:00" and request["end"] == "2027-01-05T17:00:00+01:00"
    assert request["interests"] == ["art", "history"] and "days" not in request
    assert third["brief"]["city"] == "Berlin" and "Berlin" in third["reply"]
    assert fake.queries == []  # Berlin is planned from the fixture without any lookup


def test_a_ready_request_is_accepted_by_the_trip_endpoint(tmp_path, fake_geocoder):
    with app_client(tmp_path, fake_geocoder()) as c:
        data = turn(c, "Berlin on 2027-01-05, 10 to 5, art and coffee")
        assert data["ready"] is True
        created = c.post("/api/trips", json={"request": data["request"]})
        assert created.status_code == 200, created.text
        trip = c.get(f"/api/trips/{created.json()['trip_id']}").json()["trip"]
    assert trip["request"]["city"] == "Berlin" and trip["request"]["interests"] == ["art", "coffee"]


def test_intake_rejects_extra_fields_and_oversized_or_multi_day_input(tmp_path, fake_geocoder):
    with app_client(tmp_path, fake_geocoder()) as c:
        assert c.post("/api/intake", json={"message": "Berlin", "brief": {}, "bogus": True}).status_code == 422
        assert c.post("/api/intake", json={"message": "x" * 601, "brief": {}}).status_code == 422
        assert c.post("/api/intake", json={"message": "Berlin", "brief": {"days": 3}}).status_code == 422


def test_unsupported_city_in_fixture_mode_is_explained_and_not_ready(tmp_path, geo, fake_geocoder):
    fake = fake_geocoder(geo(**PARIS))
    with app_client(tmp_path, fake) as c:
        data = turn(c, "I want to go to Paris for 3 days")
    assert data["ready"] is False and data["request"] is None
    assert data["brief"]["city"] == "Paris" and "city" not in data["missing"]
    assert any("serve_live.sh" in note for note in data["notes"])
    assert "Berlin" in data["reply"]
    assert fake.queries == ["Paris"]


def test_unknown_city_is_cleared_and_asked_again(tmp_path, fake_geocoder):
    with app_client(tmp_path, fake_geocoder(None)) as c:
        data = turn(c, "Let's do Atlantis")
    assert data["brief"]["city"] is None and "city" in data["missing"] and data["ready"] is False
    assert any("Atlantis" in note for note in data["notes"]) and "Atlantis" in data["reply"]


def test_geocoder_outage_keeps_the_city_and_berlin_still_works(tmp_path, fake_geocoder):
    down = fake_geocoder(error=ProviderError("geocoding-api.open-meteo.com request failed (ConnectError)"))
    with app_client(tmp_path, down) as c:
        first = turn(c, "Paris on 2027-01-05")
        assert first["brief"]["city"] == "Paris" and first["ready"] is False
        assert any("look up" in note for note in first["notes"])
        assert "time" in first["reply"]  # the other gaps are still asked for; the lookup is retried next turn
        second = turn(c, "Berlin instead, 10 to 5, art", first["brief"])
    assert second["brief"]["city"] == "Berlin" and second["notes"] == [] and second["ready"] is True
    assert second["request"]["city"] == "Berlin"


def test_complete_brief_with_an_unverified_city_says_how_to_retry(tmp_path, fake_geocoder):
    down = fake_geocoder(error=ProviderError("down"))
    with app_client(tmp_path, down) as c:
        data = turn(c, "Paris on 2027-01-05, 10 to 5, art")
    assert data["missing"] == [] and data["ready"] is False and data["request"] is None
    assert "retry" in data["reply"] and data["brief"]["city"] == "Paris"


def test_city_name_is_canonicalised_from_the_geocoder(tmp_path, geo, fake_geocoder):
    fake = fake_geocoder(geo(**{**KREUZBERG, "name": "Berlin-Kreuzberg"}))
    with app_client(tmp_path, fake) as c:
        data = turn(c, "kreuzberg, 2027-01-05, 10 to 5, coffee and books")
    assert fake.queries == ["Kreuzberg"]
    assert data["brief"]["city"] == "Berlin-Kreuzberg" and data["ready"] is True
    request = data["request"]
    assert request["city"] == "Berlin-Kreuzberg" and request["origin"] == {"lat": 52.49973, "lon": 13.40338}
    assert request["timezone"] == "Europe/Berlin" and request["title"] == "A day in Berlin-Kreuzberg"


def test_live_mode_plans_any_geocoded_city(tmp_path, live_env, geo, fake_geocoder):
    fake = fake_geocoder(geo(**TOKYO))
    with app_client(tmp_path, fake, data_mode="live") as c:
        config = c.get("/api/config").json()
        data = turn(c, "Tokyo on 2027-01-05, 10 to 5, art and coffee")
    assert config["intake"] is True and config["supported_cities"] is None
    assert data["ready"] is True and data["missing"] == [] and data["notes"] == []
    request = data["request"]
    assert request["city"] == "Tokyo" and request["timezone"] == "Asia/Tokyo" and request["title"] == "A day in Tokyo"
    assert request["origin"] == {"lat": 35.6895, "lon": 139.69171} and request["destination"] == request["origin"]
    assert request["start"] == "2027-01-05T10:00:00+09:00"
    assert fake.queries == ["Tokyo"]


def test_live_mode_geocodes_berlin_too(tmp_path, live_env, geo, fake_geocoder):
    berlin = geo(name="Berlin", lat=52.52437, lon=13.41053, timezone="Europe/Berlin", country="Germany")
    fake = fake_geocoder(berlin)
    with app_client(tmp_path, fake, data_mode="live") as c:
        data = turn(c, "Berlin on 2027-01-05, 10 to 5, parks")
    assert fake.queries == ["Berlin"] and data["ready"] is True
    assert data["request"]["origin"] == {"lat": 52.52437, "lon": 13.41053}


def test_config_reports_intake_and_the_fixture_city(tmp_path, fake_geocoder):
    with app_client(tmp_path, fake_geocoder()) as c:
        config = c.get("/api/config").json()
    assert config["intake"] is True and config["supported_cities"] == ["Berlin"]


def test_a_window_over_eighteen_hours_is_not_ready_but_explained(tmp_path, fake_geocoder):
    with app_client(tmp_path, fake_geocoder()) as c:
        data = turn(c, "Berlin on 2027-01-05, 9am to 8am, art")
    assert data["missing"] == [] and data["ready"] is False and data["request"] is None
    assert "18 hours" in data["reply"]
