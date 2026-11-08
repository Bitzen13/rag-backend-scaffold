from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_documented_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/health"]["get"]["summary"] == "Service health check"
