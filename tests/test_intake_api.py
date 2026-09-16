from fastapi.testclient import TestClient

from travel_agent.api import create_app


def app_client(tmp_path):
    return TestClient(create_app(str(tmp_path / "api.sqlite3"), data_mode="fixture", agent_mode="rules"))


def test_intake_requires_auth_like_other_api_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_API_TOKEN", "secret")
    with app_client(tmp_path) as c:
        response = c.post("/api/intake", json={"message": "Berlin", "brief": {}})
        assert response.status_code == 401


def test_intake_converges_to_ready_request(tmp_path):
    with app_client(tmp_path) as c:
        brief = {}
        r1 = c.post("/api/intake", json={"message": "Berlin on 5 jan for 3 days", "brief": brief})
        assert r1.status_code == 200
        data = r1.json()
        assert data["ready"] is False
        assert data["engine"] == "rules"
        brief = data["brief"]

        r2 = c.post("/api/intake", json={"message": "10 to 5", "brief": brief})
        data = r2.json()
        brief = data["brief"]
        assert data["ready"] is False

        r3 = c.post("/api/intake", json={"message": "art and history", "brief": brief})
        data = r3.json()
        assert data["ready"] is True
        assert data["request"] is not None
        assert data["request"]["city"] == "Berlin"
        assert data["missing"] == []


def test_intake_rejects_extra_fields(tmp_path):
    with app_client(tmp_path) as c:
        response = c.post("/api/intake", json={"message": "Berlin", "brief": {}, "bogus": True})
        assert response.status_code == 422


def test_intake_unsupported_city_not_ready(tmp_path):
    with app_client(tmp_path) as c:
        response = c.post("/api/intake", json={"message": "I want to go to Paris for 3 days", "brief": {}})
        data = response.json()
        assert data["ready"] is False
        assert data["request"] is None
        assert data["notes"]
