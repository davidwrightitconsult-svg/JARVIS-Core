"""
Phase 7B — strategy library, signal recording, sticky watchlist.

Principles under test:
- Each strategy is a transparent rule over 7A features; it fires only on
  ELIGIBLE markets, only on the primary outcome, and records every input.
- A signal is a hypothesis with a direction and horizon: no size, no order.
  The same strategy does not re-fire on a token while its horizon runs.
- The watchlist keeps recently-eligible markets observed after they leave
  the top-volume ranking; decided/bookless ones are dropped.
- Pipeline failures never break a market scan. Nothing can trade.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from friday.market import parse_book, parse_market, snapshot_rows
from friday.market.context import MarketContext
from friday.market.store import MarketStore
from friday.quant import LIBRARY, QuantEngine, QuantPipeline, enabled_strategies, evaluate_market
from friday.quant.features import MarketFeatures, TokenFeatures
from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
from friday.tests.test_quant_core import NOW, _book, _record

BY_NAME = {s.name: s for s in LIBRARY}


def _tok(**kw):
    base = dict(market_id="1", token_id="yes1", outcome="Yes", observed_at=NOW.isoformat(), best_bid=0.49,
                best_ask=0.51, mid=0.50, spread=0.02, half_spread=0.01, depth_usd=5000.0, top_bid_usd=500.0,
                top_ask_usd=500.0, imbalance=0.0, change_1h=0.0, change_6h=0.0, change_24h=0.0,
                volatility=0.005, range_24h=[0.49, 0.51], observations=10, usable_ratio=1.0, age_seconds=0.0,
                flags=[], usable=True)
    base.update(kw)
    return TokenFeatures(**base)


def _mkt(eligible=True, **kw):
    return MarketFeatures(market_id="1", question="Will it happen?", end_date=None, hours_to_end=100.0,
                          volume_24h=1e5, gamma_liquidity=None,
                          outcomes=[_tok(**kw), _tok(token_id="no1", outcome="No")],
                          eligible=eligible, attention=80)


def _fire(name, **kw):
    sigs = evaluate_market(_mkt(**kw), (BY_NAME[name],), run_id=7, now=NOW)
    return sigs[0] if sigs else None


# ================================================================ rules

def test_book_imbalance():
    assert _fire("BOOK_IMBALANCE", imbalance=0.5) is None
    s = _fire("BOOK_IMBALANCE", imbalance=-0.8)
    assert s["direction"] == "DOWN" and s["strength"] == 0.5 and s["inputs"]["imbalance"] == -0.8
    assert _fire("BOOK_IMBALANCE", imbalance=0.9)["direction"] == "UP"
    assert _fire("BOOK_IMBALANCE", imbalance=None) is None


def test_mean_reversion_scales_with_volatility():
    assert _fire("MEAN_REVERSION", change_1h=0.02) is None                      # below 3c floor
    s = _fire("MEAN_REVERSION", change_1h=0.04)
    assert s["direction"] == "DOWN" and s["inputs"]["threshold"] == 0.03
    assert _fire("MEAN_REVERSION", change_1h=0.04, volatility=0.02) is None     # 3 x vol = 6c
    assert _fire("MEAN_REVERSION", change_1h=-0.07, volatility=0.02)["direction"] == "UP"
    assert _fire("MEAN_REVERSION", change_1h=None) is None                      # no history: no signal


def test_momentum_needs_confirmation():
    assert _fire("MOMENTUM", change_6h=0.08, change_1h=0.02)["direction"] == "UP"
    assert _fire("MOMENTUM", change_6h=0.08, change_1h=-0.02) is None           # last hour disagrees
    assert _fire("MOMENTUM", change_6h=0.03, change_1h=0.02) is None            # trend too small
    assert _fire("MOMENTUM", change_6h=-0.10, change_1h=-0.01)["direction"] == "DOWN"
    assert _fire("MOMENTUM", change_6h=None, change_1h=0.05) is None


def test_longshot_bias():
    assert _fire("LONGSHOT_BIAS", mid=0.06)["direction"] == "DOWN"
    assert _fire("LONGSHOT_BIAS", mid=0.93)["direction"] == "UP"
    assert _fire("LONGSHOT_BIAS", mid=0.50) is None


def test_signal_shape_and_scope():
    s = _fire("BOOK_IMBALANCE", imbalance=0.8)
    assert s["token_id"] == "yes1" and s["outcome"] == "Yes"                    # primary outcome only
    assert s["run_id"] == 7 and s["horizon_hours"] == 1.0 and s["strategy_version"] == "7B.1"
    assert s["expires_at"] == (NOW + timedelta(hours=1)).isoformat(timespec="seconds")
    assert (s["mid"], s["best_bid"], s["best_ask"], s["half_spread"]) == (0.50, 0.49, 0.51, 0.01)
    assert not any(k in s for k in ("size", "stake", "order", "amount"))       # a hypothesis, not an order
    assert evaluate_market(_mkt(eligible=False, imbalance=0.9), LIBRARY) == []  # ineligible: never


def test_strategy_selection_from_env(monkeypatch):
    assert enabled_strategies() == LIBRARY
    monkeypatch.setenv("FRIDAY_QUANT_STRATEGIES", "momentum, book_imbalance")
    assert [s.name for s in enabled_strategies()] == ["BOOK_IMBALANCE", "MOMENTUM"]
    monkeypatch.setenv("FRIDAY_QUANT_STRATEGIES", "none")
    assert enabled_strategies() == ()
    monkeypatch.setenv("FRIDAY_QUANT_STRATEGIES", "GUESSWORK")
    with pytest.raises(ValueError, match="unknown"):
        enabled_strategies()


# ================================================================ pipeline

@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "d" / "m.db")


def _spike(store):
    """A sharp +6c move in the last hour on a deep, tight market."""
    for hours_ago, yes in ((2.0, 0.40), (1.2, 0.40), (0.6, 0.43), (0, 0.46)):
        _record(store, NOW - timedelta(hours=hours_ago), (yes, round(1 - yes, 2)))


def test_pipeline_records_once_per_horizon(store):
    _spike(store)
    engine = QuantEngine(store)
    pipe = QuantPipeline(engine, LIBRARY)
    first = pipe.evaluate(run_id=4, now=NOW)
    assert first["eligible"] == 1 and first["by_strategy"] == {"MEAN_REVERSION": 1} and first["recorded"]
    [sig] = store.signals(hours=1)
    assert sig["direction"] == "DOWN" and sig["inputs"]["change_1h"] == 0.06 and sig["question"]
    second = pipe.evaluate(run_id=5, now=NOW + timedelta(minutes=15))
    assert second["signals_new"] == 0 and second["suppressed_open"] == 1        # horizon still running
    assert len(store.signals(hours=1)) == 1
    assert [w["market_id"] for w in store.watchlist()] == ["1"]
    assert store.signal_counts()["MEAN_REVERSION"] == {"signals": 1, "open": 1, "up": 0, "down": 1}


def test_preview_writes_nothing(store):
    _spike(store)
    preview = QuantPipeline(QuantEngine(store), LIBRARY, record=False).evaluate(now=NOW)
    assert preview["signals_new"] == 1 and preview["signals"][0]["strategy"] == "MEAN_REVERSION"
    assert store.signals(hours=24) == [] and store.watchlist() == []


def test_signals_are_pruned_with_history(store):
    store.record_signals([{**_fire("LONGSHOT_BIAS", mid=0.05),
                           "observed_at": (NOW - timedelta(days=400)).isoformat(timespec="seconds")}])
    store.prune()
    with store._db() as db:
        assert db.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"] == 0


# ================================================================ watchlist + observers

def _deep(token, mid):
    return _fresh(_book(token, mid, 4000))


def test_watchlist_keeps_markets_observed_after_they_leave_the_ranking(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    books = {"yes1": _deep("yes1", 0.5), "no1": _deep("no1", 0.5), "y3": _deep("y3", 0.3), "n3": _deep("n3", 0.7)}
    fake = FakePolymarket([gamma_market(1), gamma_market(3, tokens=("y3", "n3"))], books)
    client = fake.client()
    client.store = store
    pipe = QuantPipeline(QuantEngine(store), LIBRARY)
    client.observers.append(pipe.on_run)
    client.tracked_source = lambda: store.watchlist_markets(48)
    _, first = client.snapshot(limit=2)
    assert first.quant["eligible"] == 2 and {w["market_id"] for w in store.watchlist()} == {"1", "3"}
    fake.markets = [gamma_market(1)]                         # market 3 drops out of the ranking
    rows, second = client.snapshot(limit=1)
    assert {r["market_id"] for r in rows} == {"1", "3"} and second.tracked == 1
    assert len(store.history("y3")) == 2                     # continuous history
    with store._db() as db:
        assert db.execute("SELECT tracked FROM runs ORDER BY run_id DESC").fetchone()["tracked"] == 1
    fake.books["y3"] = _fresh(raw_book("y3", [(0.995, 900)], [], ts=int(NOW.timestamp() * 1000)))
    fake.books["n3"] = _fresh(raw_book("n3", [], [(0.005, 900)], ts=int(NOW.timestamp() * 1000)))
    rows, third = client.snapshot(limit=1)                   # decided while watched: dropped
    assert {r["market_id"] for r in rows} == {"1"} and third.excluded_reasons == {"NEAR_CERTAIN": 1}
    assert {w["market_id"] for w in store.watchlist()} == {"1"}


def test_observer_failure_never_breaks_the_scan(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    fake = FakePolymarket([gamma_market(1)], {"yes1": _deep("yes1", 0.5), "no1": _deep("no1", 0.5)})
    client = fake.client()
    client.store = store
    seen = []
    client.observers += [seen.append, lambda run: 1 / 0]
    rows, report = client.snapshot(limit=1)
    assert len(rows) == 2 and report.stored_run == 1 and seen == [1]
    assert any("observer: ZeroDivisionError" in e for e in report.errors)
    broken = fake.client()
    broken.tracked_source = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    rows, report = broken.snapshot(limit=1)
    assert len(rows) == 2 and any("watchlist: OperationalError" in e for e in report.errors)


def test_6c_store_gains_7b_tables_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE runs(run_id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
        source TEXT NOT NULL, markets INTEGER, tokens INTEGER, books_ok INTEGER, usable_rows INTEGER,
        batch_endpoint INTEGER, errors TEXT, duration_ms INTEGER, excluded INTEGER, excluded_reasons TEXT);""")
    conn.commit()
    conn.close()
    store = MarketStore(path)
    with store._db() as db:
        assert "tracked" in {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "watchlist"} <= tables


# ================================================================ context + app

def test_claude_context_lists_open_signals(store):
    _spike(store)
    engine = QuantEngine(store)
    QuantPipeline(engine, LIBRARY).evaluate(now=NOW)
    block = MarketContext(store, engine=engine).context_for("x", ["1"])
    [sig] = block["markets"][0]["quant"]["open_signals"]
    assert sig["strategy"] == "MEAN_REVERSION" and sig["direction"] == "DOWN"
    assert "unvalidated" in block["notice"]


def test_app_signals_endpoints(tmp_path, monkeypatch):
    from friday.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    books = {"yes1": _fresh(_book("yes1", 0.06, 20000)), "no1": _fresh(_book("no1", 0.94, 20000))}
    fake = FakePolymarket([gamma_market(1)], books)
    main = _app(tmp_path, monkeypatch, fake, store)
    with TestClient(main.app) as c:
        rows = c.get("/api/scan").json()                    # live snapshot -> pipeline observer
        assert rows[0]["decision"] == "WATCH" and rows[0]["signals"] == ["LONGSHOT_BIAS:DOWN"]
        assert rows[0]["edge"] is None
        sigs = c.get("/api/quant/signals").json()
        assert [s["strategy"] for s in sigs["signals"]] == ["LONGSHOT_BIAS"] and "Not orders" in sigs["note"]
        strat = c.get("/api/quant/strategies").json()
        by = {s["name"]: s for s in strat["strategies"]}
        assert set(by) == {"BOOK_IMBALANCE", "MEAN_REVERSION", "MOMENTUM", "LONGSHOT_BIAS", "CALIBRATION_EDGE"}
        assert by["CALIBRATION_EDGE"]["judged_on"] == "resolution"
        assert by["LONGSHOT_BIAS"]["counts"]["signals"] == 1 and by["MOMENTUM"]["status"].startswith("HYPOTHESIS")
        assert strat["pipeline"]["runs"] == 1
        assert [w["market_id"] for w in c.get("/api/quant/watchlist").json()["markets"]] == ["1"]
        assert c.get("/health").json()["execution_locked"] is True
