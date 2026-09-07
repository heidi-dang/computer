from fastapi.testclient import TestClient
from cptr.app import app

client = TestClient(app)


def test_root_health_endpoint_for_deployment_broker():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}

    # Verify no leaked fields
    data = response.json()
    assert "pid" not in data
    assert "uptime_seconds" not in data


def test_health_live_endpoint():
    response = client.get("/api/health/live")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}

    # Verify no leaked fields
    data = response.json()
    assert "pid" not in data
    assert "uptime_seconds" not in data
