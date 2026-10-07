"""
Phase 5B — web articles by URL + research ingestion as Hermes jobs.

Principles under test:
- The fetcher only ever talks to public https hosts: private/loopback/
  metadata addresses, plain http, odd ports, credentials, bad redirects,
  unsupported content and oversized bodies are all refused — and it fails
  closed if it cannot confirm who it connected to.
- A fetched page becomes a library document with its URL as source,
  article text only (no nav/scripts), deduplicated by URL.
- Ingestion runs through Hermes with its own capability and is journalled.
"""

import json

import fitz
import httpx
import pytest
from fastapi.testclient import TestClient

import jarvis.research.library as library_module
from jarvis.hermes import HermesAuthorityError, HermesRuntime, HermesServices, HermesStore, TokenBudget
from jarvis.hermes.bootstrap import HermesSettings, build_hermes
from jarvis.hermes.jobs import HermesJobError, create_job
from jarvis.hermes.scheduler import HermesScheduleError, validate_schedule
from jarvis.hermes.services import JobServices
from jarvis.knowledge import KnowledgeRecorder, KnowledgeVault
from jarvis.research import ResearchLibrary
from jarvis.research.fetch import FetchError, SafeFetcher, check_url, is_public_address

PUBLIC = "93.184.215.14"
ARTICLE = ("<html><head><title>Why Prediction Markets Work</title><script>evil()</script></head>"
           "<body><nav>Home | Login | Subscribe</nav><article><h1>Why Prediction Markets Work</h1><p>"
           + "Markets aggregate dispersed information into prices that forecast outcomes. " * 10
           + "</p></article><footer>Cookie banner</footer></body></html>")


def pdf_bytes(text):
    doc = fitz.open()
    doc.new_page().insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=9)
    return doc.tobytes()


class Web:
    """Fake internet: {url: (status, headers, body)} served by httpx.MockTransport."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def handler(self, request):
        self.requests.append(str(request.url))
        status, headers, body = self.pages.get(str(request.url), (404, {"content-type": "text/plain"}, b"nope"))
        return httpx.Response(status, headers=headers, content=body)

    def fetcher(self, resolver=None, **kw):
        return SafeFetcher(transport=httpx.MockTransport(self.handler),
                           resolver=resolver or (lambda host: [PUBLIC]), verify_peer=False, **kw)


def html(body=ARTICLE):
    return (200, {"content-type": "text/html; charset=utf-8"}, body.encode())


@pytest.fixture
def lib(tmp_path):
    return ResearchLibrary(tmp_path / "library").init()


# ================================================================ URL rules

@pytest.mark.parametrize("url,why", [
    ("http://example.com/a", "only https"),
    ("ftp://example.com/a", "only https"),
    ("file:///etc/passwd", "only https"),
    ("https://user:pw@example.com/a", "credentials"),
    ("https://example.com:8443/a", "port (443)"),
    ("https://localhost/a", "not a public"),
    ("https://printer.local/a", "not a public"),
    ("https://router.lan/a", "not a public"),
    ("https:///nohost", "no host"),
    ("https://example.com/" + "a" * 2100, "too long"),
])
def test_static_url_rules(url, why):
    with pytest.raises(FetchError, match=why.replace("(", r"\(").replace(")", r"\)")):
        check_url(url)


@pytest.mark.parametrize("address,public", [
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.50", False), ("172.16.0.9", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fd00::1", False), ("::ffff:127.0.0.1", False),
    ("93.184.215.14", True), ("2606:2800:220:1::1", True), ("not-an-ip", False),
])
def test_address_classification(address, public):
    assert is_public_address(address) is public


@pytest.mark.parametrize("resolved", [["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"],
                                      [PUBLIC, "192.168.1.1"], []])
def test_non_public_resolution_is_refused_before_connecting(resolved, tmp_path):
    web = Web({"https://sneaky.example/a": html()})
    with pytest.raises(FetchError):
        web.fetcher(resolver=lambda host: resolved).fetch("https://sneaky.example/a", tmp_path)
    assert web.requests == []                                  # never connected


def test_private_ip_literal_refused(tmp_path):
    web = Web({})
    with pytest.raises(FetchError, match="non-public"):
        web.fetcher().fetch("https://192.168.1.10/admin", tmp_path)
    assert web.requests == []


def test_fails_closed_when_peer_cannot_be_verified(tmp_path):
    web = Web({"https://example.com/a": html()})
    fetcher = SafeFetcher(transport=httpx.MockTransport(web.handler), resolver=lambda h: [PUBLIC])
    with pytest.raises(FetchError, match="non-public address"):
        fetcher.fetch("https://example.com/a", tmp_path)       # no network stream -> refuse
    assert list(tmp_path.iterdir()) == []


# ================================================================ redirects, content, size

def test_relative_redirect_followed(tmp_path):
    web = Web({"https://example.com/old": (301, {"location": "/new"}, b""),
               "https://example.com/new": html()})
    got = web.fetcher().fetch("https://example.com/old", tmp_path)
    assert got.url == "https://example.com/new" and got.suffix == ".html"


@pytest.mark.parametrize("location,why", [
    ("http://example.com/plain", "only https"),
    ("https://localhost/x", "not a public"),
    ("https://example.com:8080/x", "443"),
])
def test_bad_redirect_targets_refused(tmp_path, location, why):
    web = Web({"https://example.com/a": (302, {"location": location}, b"")})
    with pytest.raises(FetchError, match=why):
        web.fetcher().fetch("https://example.com/a", tmp_path)


def test_redirect_to_host_resolving_private_refused(tmp_path):
    web = Web({"https://example.com/a": (302, {"location": "https://internal.example/x"}, b"")})
    resolver = lambda host: ["10.0.0.7"] if host == "internal.example" else [PUBLIC]
    with pytest.raises(FetchError, match="non-public"):
        web.fetcher(resolver=resolver).fetch("https://example.com/a", tmp_path)
    assert "https://internal.example/x" not in web.requests


def test_redirect_limit(tmp_path):
    pages = {f"https://example.com/{i}": (302, {"location": f"/{i + 1}"}, b"") for i in range(6)}
    with pytest.raises(FetchError, match="redirects"):
        Web(pages).fetcher().fetch("https://example.com/0", tmp_path)


@pytest.mark.parametrize("status,headers,body,why", [
    (200, {"content-type": "image/png"}, b"\x89PNG", "not supported"),
    (200, {"content-type": "application/octet-stream"}, b"x", "not supported"),
    (200, {}, b"x", "not supported"),
    (403, {"content-type": "text/html"}, b"no", "refused or rate-limited automated access"),
    (500, {"content-type": "text/html"}, b"no", "HTTP 500"),
    (200, {"content-type": "text/html", "content-length": str(10**9)}, b"x", "larger than"),
])
def test_content_rules(tmp_path, status, headers, body, why):
    web = Web({"https://example.com/a": (status, headers, body)})
    with pytest.raises(FetchError, match=why):
        web.fetcher().fetch("https://example.com/a", tmp_path)


def test_streaming_size_cap_removes_partial_file(tmp_path):
    web = Web({"https://example.com/big": (200, {"content-type": "text/plain"}, b"x" * 5000)})
    with pytest.raises(FetchError, match="larger than"):
        web.fetcher(max_bytes=1000).fetch("https://example.com/big", tmp_path / "stage")
    assert not any((tmp_path / "stage").glob("*"))


def test_no_proxy_no_env_trust(tmp_path, monkeypatch):
    seen = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:3128")
    Web({"https://example.com/a": html()}).fetcher().fetch("https://example.com/a", tmp_path)
    assert seen["trust_env"] is False and seen["follow_redirects"] is False
    assert "cookies" not in seen and "auth" not in seen


def test_user_agent_carries_contact(tmp_path, monkeypatch):
    seen = []
    web = Web({"https://example.com/a": html()})
    fetcher = SafeFetcher(transport=httpx.MockTransport(lambda r: seen.append(r.headers["user-agent"])
                                                          or web.handler(r)),
                          resolver=lambda h: [PUBLIC], verify_peer=False)
    fetcher.fetch("https://example.com/a", tmp_path)
    assert "+https://github.com/davidwrightitconsult-svg" in seen[0] and "FRIDAY-research/5B.2" in seen[0]
    monkeypatch.setenv("JARVIS_FETCH_CONTACT", "research@example.org")
    fetcher.fetch("https://example.com/a", tmp_path / "2")
    assert "+research@example.org;" in seen[1]
    monkeypatch.setenv("JARVIS_FETCH_CONTACT", "bad\r\nX-Injected: 1")
    fetcher.fetch("https://example.com/a", tmp_path / "3")
    assert "X-Injected" not in seen[2]                        # header injection impossible


def test_real_operator_data_is_isolated_in_tests():
    import os
    assert "no-library" in os.environ["JARVIS_LIBRARY_PATH"]
    assert "no-vault" in os.environ["JARVIS_VAULT_PATH"]


# ================================================================ library.add_url

def test_add_url_article_text_only(lib, monkeypatch):
    web = Web({"https://news.example/pm": html()})
    r = lib.add_url("https://news.example/pm#comments", fetcher=web.fetcher())
    assert r.status == "ADDED" and r.title == "Why Prediction Markets Work"
    doc = lib.get(r.doc_id)
    assert doc["source"] == "https://news.example/pm" and doc["kind"] == "article"
    assert lib.search("dispersed information")
    for noise in ("login subscribe", "cookie banner", "evil"):
        assert lib.search(noise) == []
    assert [p for p in lib.inbox.rglob("*") if p.is_file() or p.name.startswith(".")] == []


def test_add_url_duplicate_and_refresh(lib):
    web = Web({"https://news.example/pm": html()})
    first = lib.add_url("https://news.example/pm", fetcher=web.fetcher())
    assert lib.add_url("https://news.example/pm", fetcher=web.fetcher()).status == "DUPLICATE"
    assert len(web.requests) == 1                               # duplicate never re-fetched
    web.pages["https://news.example/pm"] = html(ARTICLE.replace("aggregate", "combine"))
    fresh = lib.add_url("https://news.example/pm", refresh=True, fetcher=web.fetcher())
    assert fresh.status == "ADDED" and fresh.doc_id != first.doc_id
    assert lib.get(first.doc_id) is None and lib.status()["documents"] == 1


def test_add_url_pdf_paper_and_rejections(lib):
    web = Web({"https://arxiv.example/paper.pdf":
               (200, {"content-type": "application/pdf"}, pdf_bytes("Calibration of forecasters. " * 20))})
    r = lib.add_url("https://arxiv.example/paper.pdf", kind="paper", fetcher=web.fetcher())
    assert r.status == "ADDED" and lib.get(r.doc_id)["format"] == "pdf"
    bad = lib.add_url("http://arxiv.example/x.pdf", fetcher=web.fetcher())
    assert bad.status == "REJECTED" and "only https" in bad.reason
    missing = lib.add_url("https://arxiv.example/missing", fetcher=web.fetcher())
    assert missing.status == "REJECTED" and "HTTP 404" in missing.reason


def test_inbox_kind_subfolders(lib, tmp_path):
    (lib.inbox / "paper" / "p.md").write_text("# Paper\n\n" + "Longshot bias evidence. " * 12)
    (lib.inbox / "note" / "n.md").write_text("# Note\n\n" + "My own calibration notes. " * 12)
    (lib.inbox / "b.md").write_text("# Book\n\n" + "Trading in the zone discipline. " * 12)
    results = lib.ingest_inbox()
    assert sorted(r.status for r in results) == ["ADDED"] * 3
    assert lib.status()["by_kind"] == {"book": 1, "note": 1, "paper": 1}
    assert lib.status()["inbox_waiting"] == 0


# ================================================================ Hermes

class MockedLibrary:
    """The real library, with every URL fetch going to the fake internet."""

    def __init__(self, lib, web):
        self.lib, self.web = lib, web

    def add_url(self, url, **kw):
        return self.lib.add_url(url, fetcher=self.web.fetcher(), **kw)

    def ingest_inbox(self, kind="book"):
        return self.lib.ingest_inbox(kind)


def _runtime(tmp_path, research=None, recorder=None):
    store = HermesStore(tmp_path / "h.db")
    return HermesRuntime(store, TokenBudget(store), HermesServices(research=research),
                         on_job_finished=recorder.record_job if recorder else None)


def test_url_job_ingests_and_journals(tmp_path, lib):
    vault = KnowledgeVault(tmp_path / "vault")
    vault.init()
    web = Web({"https://news.example/pm": html()})
    rt = _runtime(tmp_path, MockedLibrary(lib, web), KnowledgeRecorder(vault))
    job = rt.submit({"job_type": "RESEARCH_INGEST_URL", "requested_by": "API",
                     "params": {"url": "https://news.example/pm", "kind": "article"}})
    rt.run_pending()
    assert job.status.value == "SUCCEEDED" and job.result["status"] == "ADDED"
    assert job.result["execution_authorized"] is False
    journal = (vault.root / vault.list_notes("FRIDAY/Journal")[0]).read_text()
    assert "**RESEARCH** ADDED «Why Prediction Markets Work» from `https://news.example/pm`" in journal


def test_refused_url_is_a_normal_rejected_result(tmp_path, lib):
    web = Web({})
    rt = _runtime(tmp_path, MockedLibrary(lib, web))
    job = rt.submit({"job_type": "RESEARCH_INGEST_URL", "params": {"url": "https://192.168.1.1/admin"}})
    rt.run_pending()
    assert job.status.value == "SUCCEEDED" and job.result["status"] == "REJECTED"
    assert "non-public" in job.result["reason"] and job.attempts == 1       # no retries


@pytest.mark.parametrize("params", [
    {"url": "http://example.com/a"}, {"url": "https://example.com/a", "kind": "strategy"},
    {"url": "https://example.com/a", "execute": True}, {},
])
def test_url_job_params_validated(params):
    with pytest.raises(HermesJobError):
        create_job({"job_type": "RESEARCH_INGEST_URL", "params": params})


def test_ingest_capability_gate(tmp_path, lib):
    services = HermesServices(research=lib)
    scan = JobServices(create_job({"job_type": "MARKET_SCAN"}), services)
    with pytest.raises(HermesAuthorityError):
        scan.research_library()
    inbox = JobServices(create_job({"job_type": "RESEARCH_INGEST_INBOX"}), services)
    assert inbox.research_library() is lib


def test_inbox_job_and_schedule(tmp_path, lib):
    (lib.inbox / "paper" / "p.md").write_text("# P\n\n" + "Evidence of calibration. " * 12)
    rt = _runtime(tmp_path, lib)
    job = rt.submit({"job_type": "RESEARCH_INGEST_INBOX"})
    rt.run_pending()
    assert job.result["added"] == 1 and job.result["results"][0]["title"] == "P"
    with pytest.raises(HermesScheduleError):
        validate_schedule({"name": "x", "job": {"job_type": "RESEARCH_INGEST_INBOX"}, "interval_seconds": 60})

    from jarvis.core.health import HealthManager
    from jarvis.core.state import StateManager
    state = StateManager("PAPER")

    def build(research):
        return build_hermes(
            settings=HermesSettings(health_interval_seconds=0, market_scan_interval_seconds=0,
                                    research_inbox_interval_seconds=600),
            db_path=tmp_path / f"s{bool(research)}.db", state_snapshot=state.snapshot,
            health_check=HealthManager(state).check, event_sink=lambda e: None,
            set_component=state.set_component, record_error=state.record_error, research=research)

    assert [s["name"] for s in build(lib).scheduler.list()] == ["research-inbox"]
    assert build(None).scheduler.list() == []


# ================================================================ app

@pytest.fixture
def app_web(tmp_path, monkeypatch):
    import jarvis.app.db as db_module
    import jarvis.app.main as main
    from jarvis.core.health import HealthManager
    from jarvis.core.lifecycle import LifecycleManager
    from jarvis.core.state import StateManager
    from jarvis.tests.test_app_hermes import FakeIntelligence, FakeMarket

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "jarvis.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    monkeypatch.setattr(main, "INTELLIGENCE_FACTORY", FakeIntelligence)
    monkeypatch.setattr(main, "MARKET_FACTORY", FakeMarket)
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("JARVIS_KNOWLEDGE_ENABLED", "false")
    lib = ResearchLibrary(tmp_path / "library").init()
    monkeypatch.setenv("JARVIS_LIBRARY_PATH", str(lib.root))
    web = Web({"https://news.example/pm": html()})
    monkeypatch.setattr(library_module, "SafeFetcher", lambda: web.fetcher())
    return main, lib


def test_app_url_inbox_documents(app_web):
    main, lib = app_web
    (lib.inbox / "note" / "n.md").write_text("# N\n\n" + "Base rates first. " * 20)
    with TestClient(main.app) as client:
        schedules = [s["name"] for s in client.get("/api/hermes/status").json()["schedules"]]
        assert "research-inbox" in schedules
        added = client.post("/api/research/url", json={"url": "https://news.example/pm"}).json()
        assert added["status"] == "SUCCEEDED" and added["result"]["status"] == "ADDED"
        refused = client.post("/api/research/url", json={"url": "http://news.example/pm"})
        assert refused.status_code == 400 and "https" in refused.json()["detail"]
        inbox = client.post("/api/research/ingest-inbox").json()
        assert inbox["result"]["count"] in (0, 1)           # the startup schedule may have run first
        docs = client.get("/api/research/documents").json()
    assert {d["kind"] for d in docs} == {"article", "note"}
    assert all("original" not in d for d in docs)             # no server paths exposed
    assert next(d for d in docs if d["kind"] == "article")["source"] == "https://news.example/pm"
