"""Offline tests for GET /api/geocode and the geocoding keys of /api/config. No test touches the network."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from travel_agent import api as api_module
from travel_agent.api import create_app
from travel_agent.providers.services import ProviderError
from travel_agent.schemas import Coordinate, Evidence, GeoPlace

TOKYO = {"query": "Tokyo", "name": "Tokyo", "country": "Japan", "admin1": "Tokyo", "lat": 35.6895,
         "lon": 139.69171, "timezone": "Asia/Tokyo", "population": 9733276}
BERLIN = {"query": "Berlin", "name": "Berlin", "country": "Germany", "admin1": "Land Berlin", "lat": 52.52437,
          "lon": 13.41053, "timezone": "Europe/Berlin", "population": 3426354}


def place(*, lat, lon, status="live", **fields):
    """A GeoPlace-shaped result as the Geocoder would build it from an Open-Meteo record."""
    data = {"country": "", "admin1": "", "population": None, **fields, "coordinate": Coordinate(lat=lat, lon=lon),
            "source": Evidence(id="geocode-" + fields["name"].lower(), provider="Open-Meteo geocoding", status=status,
                               retrieved_at=datetime.now(UTC), reference="https://open-meteo.com/en/docs/geocoding-api")}
    return GeoPlace(**data)


class FakeGeocoder:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.queries = result, error, []

    def search(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.result


def client(tmp_path, geocoder):
    return TestClient(create_app(str(tmp_path / "geocode.sqlite3"), data_mode="fixture", agent_mode="rules",
                                 geocoder=geocoder))


def test_geocode_returns_the_documented_shape_and_strips_the_query(tmp_path):
    fake = FakeGeocoder(place(**TOKYO))
    with client(tmp_path, fake) as c:
        response = c.get("/api/geocode", params={"q": "  Tokyo  "})
    assert response.status_code == 200
    assert response.json() == {"query": "Tokyo", "name": "Tokyo", "country": "Japan", "admin1": "Tokyo",
                               "lat": 35.6895, "lon": 139.69171, "timezone": "Asia/Tokyo", "population": 9733276,
                               "fixture_supported": False,
                               "source": {"provider": "Open-Meteo geocoding", "status": "live"}}
    assert fake.queries == ["Tokyo"]


def test_berlin_result_is_fixture_supported_and_cached_status_passes_through(tmp_path):
    with client(tmp_path, FakeGeocoder(place(status="cached", **BERLIN))) as c:
        body = c.get("/api/geocode?q=Berlin").json()
    assert body["fixture_supported"] is True
    assert body["timezone"] == "Europe/Berlin" and body["admin1"] == "Land Berlin"
    assert body["source"] == {"provider": "Open-Meteo geocoding", "status": "cached"}


def test_population_may_be_null(tmp_path):
    village = place(query="Tokyo", name="Tokyo", country="Papua New Guinea", lat=-6.05, lon=147.6, timezone="Pacific/Port_Moresby")
    with client(tmp_path, FakeGeocoder(village)) as c:
        body = c.get("/api/geocode?q=Tokyo").json()
    assert body["population"] is None and body["admin1"] == "" and body["fixture_supported"] is False


def test_no_match_is_404_without_a_guessed_location(tmp_path):
    with client(tmp_path, FakeGeocoder(None)) as c:
        response = c.get("/api/geocode?q=Nowhereville")
    assert response.status_code == 404
    assert response.json() == {"detail": "No place matched that name"}


@pytest.mark.parametrize("query", ["T", "   ", "x" * 81])
def test_invalid_query_is_400_before_the_geocoder_is_called(tmp_path, query):
    fake = FakeGeocoder(place(**TOKYO))
    with client(tmp_path, fake) as c:
        response = c.get("/api/geocode", params={"q": query})
    assert response.status_code == 400 and response.json()["detail"]
    assert fake.queries == []


def test_missing_query_is_400(tmp_path):
    with client(tmp_path, FakeGeocoder(place(**TOKYO))) as c:
        assert c.get("/api/geocode").status_code == 400


def test_geocoder_value_error_is_400_with_its_message(tmp_path):
    with client(tmp_path, FakeGeocoder(error=ValueError("Query contains unsupported characters"))) as c:
        response = c.get("/api/geocode?q=Tokyo")
    assert response.status_code == 400
    assert response.json() == {"detail": "Query contains unsupported characters"}


def test_provider_error_is_503_and_hides_the_upstream_detail(tmp_path):
    error = ProviderError("geocoding-api.open-meteo.com request failed (502) upstream-detail")
    with client(tmp_path, FakeGeocoder(error=error)) as c:
        response = c.get("/api/geocode?q=Tokyo")
    assert response.status_code == 503
    assert response.json() == {"detail": "Geocoding service unavailable"}
    assert "upstream-detail" not in response.text


def test_config_reports_geocoding_router_and_live_notice_in_fixture_mode(tmp_path):
    with client(tmp_path, FakeGeocoder()) as c:
        config = c.get("/api/config").json()
    assert config["geocoding"] is True
    assert config["router"] is None
    assert config["live_notice"].startswith("Live OpenStreetMap data. Prices and hours stay unknown")
    assert config["data_mode"] == "fixture" and config["fixture_notice"].startswith("Synthetic Berlin scenario")


def test_geocoder_is_built_lazily_once_and_reused(tmp_path, monkeypatch):
    fake, built = FakeGeocoder(place(**TOKYO)), []
    monkeypatch.setattr(api_module, "make_geocoder", lambda: built.append(fake) or fake)
    with client(tmp_path, None) as c:
        assert built == []
        assert c.get("/api/config").status_code == 200 and built == []
        assert c.get("/api/geocode?q=Tokyo").status_code == 200
        assert c.get("/api/geocode?q=Tokyo").status_code == 200
    assert len(built) == 1 and fake.queries == ["Tokyo", "Tokyo"]


def test_geocode_requires_the_api_token_when_one_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_API_TOKEN", "test-secret")
    with client(tmp_path, FakeGeocoder(place(**TOKYO))) as c:
        assert c.get("/api/geocode?q=Tokyo").status_code == 401
        assert c.get("/api/geocode?q=Tokyo", headers={"Authorization": "Bearer test-secret"}).status_code == 200


def test_live_mode_with_a_broken_router_configuration_fails_at_startup(tmp_path, monkeypatch):
    for name in ("ORS_API_KEY", "TRAVEL_CONTACT", "TRAVEL_ALLOW_PUBLIC_OVERPASS", "OSRM_URL_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRAVEL_ROUTER", "carrier-pigeon")
    with pytest.raises(ValueError, match="misconfigured"):
        create_app(str(tmp_path / "live.sqlite3"), data_mode="live", agent_mode="rules", geocoder=FakeGeocoder())


def test_fixture_mode_never_builds_a_live_provider_at_startup(tmp_path, monkeypatch):
    def explode(mode):
        raise AssertionError("make_provider must not run at startup in fixture mode")
    monkeypatch.setattr(api_module, "make_provider", explode)
    with client(tmp_path, FakeGeocoder()) as c:
        assert c.get("/api/config").json()["router"] is None
