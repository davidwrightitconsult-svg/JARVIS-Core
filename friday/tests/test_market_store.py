"""
Phase 6B — market data store: history, book levels, stats, retention.

Principles under test:
- Every snapshot run becomes durable history (runs, markets, outcomes,
  per-token snapshots, top-of-book levels) without bloating Hermes jobs.
- Storage problems never break a scan; retention keeps the disk bounded;
  the database never lives inside a Git repo.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from friday.hermes import HermesRuntime, HermesServices, HermesStore, TokenBudget
from friday.market import parse_book, parse_market, snapshot_rows
from friday.market.store import MarketStore, MarketStoreError
from friday.tests.test_market_data import BOOKS, FakePolymarket, GOOD_NO, GOOD_YES, gamma_market


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "data" / "market_data.db")


def _fake(store=None, markets=None):
    fake = FakePolymarket(markets or [gamma_market(1), gamma_market(2, tokens=("yes2", "no2"), bid=0.01, ask=0.99)],
                          BOOKS)
    client = fake.client()
    client.store = store
    return fake, client


# ================================================================ recording

def test_snapshot_is_recorded_with_levels(store):
    _, client = _fake(store)
    rows, report = client.snapshot(limit=10)
    assert report.stored_run == 1 and report.errors == []
    assert all("top_bids" not in r and "top_asks" not in r for r in rows)      # job results stay small
    stats = store.stats()
    assert stats["counts"] == {"runs": 1, "markets": 2, "outcomes": 4, "snapshots": 4,
                               "book_levels": stats["counts"]["book_levels"]}
    assert stats["counts"]["book_levels"] > 0
    lv = store.levels("yes1")
    assert lv["bids"][0] == [0.48, 100.0] and lv["asks"][0] == [0.50, 100.0]   # best first
    assert len(lv["bids"]) <= 5 and [p for p, _ in lv["bids"]] == sorted((p for p, _ in lv["bids"]), reverse=True)


def test_history_accumulates_and_markets_upsert(store):
    _, client = _fake(store)
    client.snapshot(limit=10)
    client.snapshot(limit=10)
    hist = store.history("yes1")
    assert len(hist) == 2 and hist[0]["mid"] == 0.49 and hist[0]["usable"] is True
    assert hist[0]["flags"] == [] and store.history("yes2")[0]["flags"] == ["PLACEHOLDER"]
    markets = store.markets()
    assert len(markets) == 2 and {o["name"] for o in markets[0]["outcomes"]} == {"Yes", "No"}
    assert all(m["snapshots"] == 4 for m in markets)


def test_stats_quality_and_depth_check(store):
    _, client = _fake(store)
    client.snapshot(limit=10)
    s = store.stats()
    assert s["usable_ratio"] == 0.5 and s["flags"] == {"PLACEHOLDER": 2}
    assert s["depth_vs_gamma_liquidity"]["markets"] == 1          # 6C: usable markets only (placeholder skipped)
    assert s["depth_vs_gamma_liquidity"]["median_ratio"] is not None
    assert s["runs"] == 1 and s["runs_with_errors"] == 0 and s["size_mb"] >= 0


def test_retention_prunes_levels_before_snapshots(store):
    m = parse_market(gamma_market())
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=45)
    rows = snapshot_rows(m, {"yes1": parse_book(GOOD_YES), "no1": parse_book(GOOD_NO)}, now=old)
    store.record([m], rows, {"observed_at": old.isoformat(timespec="seconds"), "markets": 1})
    ancient = now - timedelta(days=400)
    rows2 = snapshot_rows(m, {"yes1": parse_book(GOOD_YES), "no1": parse_book(GOOD_NO)}, now=ancient)
    store.record([m], rows2, {"observed_at": ancient.isoformat(timespec="seconds"), "markets": 1})
    removed = store.prune(now)
    assert removed["snapshots"] == 2 and removed["runs"] == 1          # 400-day-old run gone
    assert removed["book_levels"] > 0
    with store._db() as db:
        assert db.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"] == 2      # 45-day-old kept
        assert db.execute("SELECT COUNT(*) n FROM book_levels").fetchone()["n"] == 0    # levels > 30 days gone


def test_store_refuses_git_repo_and_bad_retention(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    with pytest.raises(MarketStoreError, match="inside the Git repository"):
        MarketStore(tmp_path / "repo" / "data" / "m.db")
    with pytest.raises(MarketStoreError):
        MarketStore(tmp_path / "m.db", retention_days=0)


def test_store_failure_never_breaks_the_scan(store, monkeypatch):
    _, client = _fake(store)
    monkeypatch.setattr(store, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rows, report = client.snapshot(limit=10)
    assert len(rows) == 4 and report.stored_run is None
    assert any("store: OSError: disk full" in e for e in report.errors)


def test_hermes_market_scan_records_history(tmp_path, store):
    _, client = _fake(store)
    hs = HermesStore(tmp_path / "h.db")
    rt = HermesRuntime(hs, TokenBudget(hs), HermesServices(market=client))
    job = rt.submit({"job_type": "MARKET_SCAN", "params": {"limit": 4}})
    rt.run_pending()
    assert job.status.value == "SUCCEEDED" and job.result["count"] == 4
    assert "top_bids" not in json.dumps(job.result)
    assert store.stats()["counts"]["snapshots"] == 4


# ================================================================ app + builder

def test_build_market_store_from_env(tmp_path, monkeypatch):
    from friday.app.polymarket import build_market_store
    monkeypatch.setenv("FRIDAY_MARKET_DB", str(tmp_path / "d" / "m.db"))
    store, status = build_market_store()
    assert store is not None and status["enabled"] is True
    monkeypatch.setenv("FRIDAY_MARKET_STORE", "false")
    assert build_market_store()[0] is None
    monkeypatch.setenv("FRIDAY_MARKET_STORE", "true")
    monkeypatch.setenv("FRIDAY_MARKET_RETENTION_DAYS", "0")
    store, status = build_market_store()
    assert store is None and "retention" in status["reason"]


def _app(tmp_path, monkeypatch, client):
    import friday.app.db as db_module
    import friday.app.main as main
    from friday.core.health import HealthManager
    from friday.core.lifecycle import LifecycleManager
    from friday.core.state import StateManager
    from friday.tests.test_app_hermes import FakeIntelligence

    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "friday.db"))
    state = StateManager("PAPER")
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "health_manager", HealthManager(state))
    monkeypatch.setattr(main, "lifecycle", LifecycleManager(state, main.events))
    monkeypatch.setattr(main, "INTELLIGENCE_FACTORY", FakeIntelligence)
    monkeypatch.setattr(main, "MARKET_FACTORY", lambda: client)
    for name in ("HERMES_HEALTH_INTERVAL_SECONDS", "HERMES_MARKET_SCAN_INTERVAL_SECONDS"):
        monkeypatch.setenv(name, "0")
    return main


def test_app_market_endpoints(tmp_path, monkeypatch, store):
    _, client = _fake(store)
    main = _app(tmp_path, monkeypatch, client)
    with TestClient(main.app) as c:
        c.get("/api/scan")
        stats = c.get("/api/market/stats").json()
        markets = c.get("/api/market/markets").json()
        hist = c.get("/api/market/history", params={"token_id": "yes1"}).json()
        levels = c.get("/api/market/levels", params={"token_id": "yes1"}).json()
        report = c.get("/api/market/report").json()
    assert stats["counts"]["runs"] == 1 and len(markets) == 2
    assert hist["rows"][0]["best_bid"] == 0.48 and levels["bids"][0] == [0.48, 100.0]
    assert report["stored_run"] == 1


def test_app_without_store(tmp_path, monkeypatch):
    _, client = _fake(None)
    main = _app(tmp_path, monkeypatch, client)
    with TestClient(main.app) as c:
        assert c.get("/api/market/stats").status_code == 503
        assert c.get("/health").json()["status"] == "HEALTHY"
