"""Phase 13D: a write needs the shared secret. Reads do not."""

import pytest
from fastapi.testclient import TestClient

from jarvis.app import main
from jarvis.app.auth import auth_status, configured_token

TOKEN = "s3cret-token-at-least-16-chars"
KILL = {"reason": "test"}


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", TOKEN)
    with TestClient(main.app) as c:
        yield c


def test_a_write_without_the_token_is_refused(secured):
    r = secured.post("/api/risk/kill", json=KILL)
    assert r.status_code == 401 and r.headers["X-JARVIS-Auth"] == "required"
    assert "needs the API token" in r.json()["detail"]
    assert secured.get("/api/risk/status").json()["kill_switch"]["engaged"] is False   # nothing happened


def test_both_header_forms_are_accepted(secured):
    for headers in ({"Authorization": f"Bearer {TOKEN}"}, {"X-JARVIS-Token": TOKEN}):
        r = secured.post("/api/risk/resume", json={"confirmed_by": "owner"}, headers=headers)
        assert r.status_code != 401 and r.headers["X-JARVIS-Auth"] == "ok"


def test_a_wrong_or_empty_token_is_refused(secured):
    for headers in ({"Authorization": "Bearer wrong-token-wrong-token"}, {"X-JARVIS-Token": ""},
                    {"Authorization": "Bearer "}, {"Authorization": f"Basic {TOKEN}"}):
        assert secured.post("/api/risk/kill", json=KILL, headers=headers).status_code == 401


def test_a_token_that_is_merely_a_prefix_is_refused(secured):
    assert secured.post("/api/risk/kill", json=KILL,
                        headers={"X-JARVIS-Token": TOKEN[:-1]}).status_code == 401
    assert secured.post("/api/risk/kill", json=KILL,
                        headers={"X-JARVIS-Token": TOKEN + "x"}).status_code == 401


def test_reads_stay_open_so_the_dashboard_works(secured):
    for path in ("/health", "/api/dashboard", "/api/paper", "/api/risk/status", "/api/trigger/status"):
        assert secured.get(path).status_code == 200


def test_every_control_route_is_covered(secured):
    """The routes that stop, restart or resume the platform must all refuse an unauthenticated write."""
    for path, body in (("/api/risk/kill", KILL), ("/api/risk/resume", {"confirmed_by": "x"}),
                       ("/api/hermes/kill", KILL), ("/api/hermes/reset", {"confirmed_by": "x"}),
                       ("/api/hermes/pause", {}), ("/api/hermes/resume", {}),
                       ("/api/directives/resume", {"confirm": "yes"}), ("/api/directives/set", {"key": "k", "value": 1}),
                       ("/api/directives/directive", {"text": "stop after 3 losses"}),
                       ("/api/command", {"text": "hello"})):
        assert secured.post(path, json=body).status_code == 401, path


def test_a_browser_style_cross_site_post_is_refused(secured):
    """A page on the open internet can reach 127.0.0.1; coming from loopback is not a credential."""
    r = secured.post("/api/hermes/kill", json=KILL,
                     headers={"Origin": "https://evil.example", "Referer": "https://evil.example/"})
    assert r.status_code == 401


def test_unset_token_keeps_the_old_behaviour_but_says_so(monkeypatch):
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    assert configured_token() is None
    with TestClient(main.app) as c:
        r = c.post("/api/risk/kill", json=KILL)
        assert r.status_code != 401 and r.headers["X-JARVIS-Auth"] == "disabled"
        status = c.get("/health").json()["api_auth"]
        assert status["enabled"] is False and "kill switch" in status["reason"]
        c.post("/api/risk/resume", json={"confirmed_by": "owner"})


def test_health_reports_the_state_of_authentication(secured):
    assert secured.get("/health").json()["api_auth"] == {"enabled": True, "protects": "writes"}


def test_a_short_token_is_enforced_but_flagged_as_weak(monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "short")
    assert auth_status()["weak"] is True and auth_status()["enabled"] is True
    with TestClient(main.app) as c:
        assert c.post("/api/risk/kill", json=KILL).status_code == 401
        assert c.get("/health").json()["api_auth"]["weak"] is True


def test_whitespace_only_token_counts_as_unset(monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "   ")
    assert configured_token() is None and auth_status()["enabled"] is False
