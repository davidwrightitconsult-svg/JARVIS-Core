"""Phase 8a: JARVIS API version endpoint tests.

Verifies:
- The routers folder is wired into the FastAPI app
- GET /api/version returns the expected JARVIS identity payload
- The existing /health endpoint is not broken by the router addition
"""

from fastapi.testclient import TestClient

from jarvis.app.main import app


client = TestClient(app)


def test_version_endpoint_returns_200():
    resp = client.get("/api/version")
    assert resp.status_code == 200


def test_version_endpoint_payload_shape():
    resp = client.get("/api/version")
    body = resp.json()
    assert set(body.keys()) == {"name", "version", "phase"}
    assert body["name"] == "JARVIS"
    assert body["version"] == "0.1.0"
    assert body["phase"] == "8a"


def test_version_endpoint_content_type_is_json():
    resp = client.get("/api/version")
    assert resp.headers["content-type"].startswith("application/json")


def test_health_endpoint_still_works_after_router_added():
    """Regression guard: the router import must not break existing routes."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    # health is a dict; we only check it's not empty and is JSON-serializable
    assert isinstance(body, dict)
    assert len(body) > 0
