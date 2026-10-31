"""Phase 13C: the gate's judgement finally reaches the engine — and only the bet it approved."""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper.costs import CostSettings
from jarvis.paper import PaperEngine, PaperSettings, SignalTrader
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

YES, NO, MKT = "yes1", "no1", "m1"
GATE = {"approved_for_paper": ["MOMENTUM"], "forward_test": ["MEAN_REVERSION"], "live_approved": []}


def iso(dt):
    return dt.isoformat(timespec="seconds")


@pytest.fixture
def rig(tmp_path):
    store = MarketStore(tmp_path / "market.db")
    now = datetime.now(timezone.utc)
    at = iso(now)
    with store._db() as db:
        db.execute("INSERT INTO markets(market_id, question, first_seen, last_seen) VALUES(?,?,?,?)",
                   (MKT, "Will it?", at, at))
        db.executemany("INSERT INTO outcomes(token_id, market_id, name) VALUES(?,?,?)",
                       [(YES, MKT, "Yes"), (NO, MKT, "No")])
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        for tok, bid, ask in ((YES, 0.48, 0.50), (NO, 0.49, 0.51)):
            cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask,"
                             " mid, spread, flags, usable) VALUES(1,?,?,?,?,?,?,?,'[]',1)",
                             (at, tok, MKT, bid, ask, (bid + ask) / 2, ask - bid))
            # 14A: a market with no recorded book is not tradeable. Deep levels here keep these
            # tests about signal selection rather than about the fill model.
            db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                           " VALUES(?,?,?,?,?,?)",
                           [(cur.lastrowid, "bid", 0, bid, 2000.0, at), (cur.lastrowid, "ask", 0, ask, 2000.0, at)])
    state = RiskState(tmp_path / "risk.db")
    gate = {"approved_for_paper": ["MOMENTUM"], "forward_test": ["MEAN_REVERSION"], "live_approved": []}
    engine = PaperEngine(RiskGovernor(RiskSettings(), state, lambda: gate), PaperLedger(tmp_path / "risk.db", 1000.0),
                         # 14B charges a taker fee on every fill. These tests are about signal trading, so fees are off here:
    # otherwise every expected price would carry an unrelated term. See test_paper_costs.py.
    store, PaperSettings(costs=CostSettings(enabled=False)))
    return store, engine, gate


def signal(store, strategy="MOMENTUM", direction="UP", token=YES, horizon=6.0, age_minutes=0.0):
    now = datetime.now(timezone.utc)
    observed = now - timedelta(minutes=age_minutes)
    return store.record_signals([{
        "run_id": 1, "observed_at": iso(observed), "strategy": strategy, "strategy_version": "1",
        "market_id": MKT, "token_id": token, "outcome": "Yes", "direction": direction, "strength": 0.8,
        "horizon_hours": horizon, "expires_at": iso(observed + timedelta(hours=horizon)),
        "mid": 0.49, "best_bid": 0.48, "best_ask": 0.50, "half_spread": 0.01, "inputs": {}}])[0]


def trader(engine, store, gate, **kw):
    return SignalTrader(engine, store, gate_provider=lambda: gate, enabled=True, **kw)


def test_an_up_signal_becomes_a_position_with_no_stop_and_no_target(rig):
    store, engine, gate = rig
    sid = signal(store)
    out = trader(engine, store, gate).on_run()
    assert out["placed"] == 1 and out["orders"][0]["signal_id"] == sid
    pos = engine.ledger.positions("OPEN")[0]
    assert pos["instrument"] == YES and pos["strategy"] == "MOMENTUM" and pos["entry_price"] == 0.50
    plan = engine._plans()[pos["position_id"]]
    # Unreachable: the measured bet has no stop. A stop distance of 0.999 would put the stop
    # price below zero, so it is clamped to 0 -- a bid can never reach that either.
    assert plan["target_price"] is None and plan["stop_price"] == 0.0
    assert engine.tick()["closed"] == []                                 # and nothing closes it early


def test_a_down_signal_buys_the_other_outcome(rig):
    store, engine, gate = rig
    signal(store, direction="DOWN", token=YES)
    assert trader(engine, store, gate).on_run()["placed"] == 1
    assert engine.ledger.positions("OPEN")[0]["instrument"] == NO


def test_the_position_expires_at_the_signals_own_horizon(rig):
    store, engine, gate = rig
    signal(store, horizon=6.0)
    trader(engine, store, gate).on_run()
    plan = engine._plans()[engine.ledger.positions("OPEN")[0]["position_id"]]
    due = datetime.fromisoformat(plan["expires_at"])
    assert timedelta(hours=5.9) < due - datetime.now(timezone.utc) < timedelta(hours=6.1)


def test_only_strategies_the_gate_allows_are_traded(rig):
    store, engine, gate = rig
    signal(store, strategy="MOMENTUM")                    # approved
    signal(store, strategy="MEAN_REVERSION", token=NO)    # forward test
    signal(store, strategy="LONGSHOT_BIAS")               # not approved: must not be considered
    out = trader(engine, store, gate).on_run()
    assert out["placed"] == 2 and out["considered"] == 2
    assert sorted(p["strategy"] for p in engine.ledger.positions("OPEN")) == ["MEAN_REVERSION", "MOMENTUM"]


def test_nothing_trades_when_the_gate_approves_nothing(rig):
    store, engine, _ = rig
    signal(store)
    empty = {"approved_for_paper": [], "forward_test": [], "live_approved": []}
    out = SignalTrader(engine, store, gate_provider=lambda: empty, enabled=True).on_run()
    assert out["placed"] == 0 and "the gate allows no strategy" in out["inactive"]
    assert engine.ledger.positions("OPEN") == []


def test_a_broken_or_missing_gate_fails_closed(rig):
    store, engine, _ = rig
    signal(store)
    for provider in (None, (lambda: 1 / 0), (lambda: "not a gate")):
        t = SignalTrader(engine, store, gate_provider=provider, enabled=True)
        assert t.allowed_strategies() == set() and t.on_run()["placed"] == 0
    assert engine.ledger.positions("OPEN") == []


def test_a_signal_is_traded_once_even_across_restarts(rig):
    store, engine, gate = rig
    signal(store)
    assert trader(engine, store, gate).on_run()["placed"] == 1
    fresh = SignalTrader(engine, store, gate_provider=lambda: gate, enabled=True)   # new object, same db
    out = fresh.on_run()
    assert out["placed"] == 0 and out["considered"] == 0


def test_a_stale_signal_is_skipped_not_entered_late(rig):
    store, engine, gate = rig
    signal(store, age_minutes=120)
    out = trader(engine, store, gate).on_run()
    assert out["skipped"] == 1 and out["placed"] == 0
    assert "older than the entry window" in trader(engine, store, gate).report()["recent"][0]["note"]


def test_a_signal_whose_horizon_has_almost_run_out_is_skipped(rig):
    store, engine, gate = rig
    signal(store, horizon=0.1)
    assert trader(engine, store, gate).on_run()["skipped"] == 1


def test_down_is_skipped_when_the_market_is_not_binary(rig):
    store, engine, gate = rig
    with store._db() as db:
        db.execute("INSERT INTO outcomes(token_id, market_id, name) VALUES('maybe1',?, 'Maybe')", (MKT,))
    signal(store, direction="DOWN")
    out = trader(engine, store, gate).on_run()
    assert out["skipped"] == 1 and out["placed"] == 0


def test_a_refusal_is_recorded_and_not_retried(rig):
    store, engine, gate = rig
    engine.settings = PaperSettings(max_spread=0.001, costs=CostSettings(enabled=False))      # the engine will refuse: spread too wide
    signal(store)
    out = trader(engine, store, gate).on_run()
    assert out["refused"] == 1 and out["placed"] == 0
    assert trader(engine, store, gate).on_run()["considered"] == 0      # recorded, never retried


def test_disabled_does_nothing(rig):
    store, engine, gate = rig
    signal(store)
    t = SignalTrader(engine, store, gate_provider=lambda: gate, enabled=False)
    assert t.on_run()["inactive"] == "disabled" and engine.ledger.positions("OPEN") == []


def test_the_report_counts_what_happened(rig):
    store, engine, gate = rig
    signal(store)
    signal(store, strategy="LONGSHOT_BIAS", token=NO)
    t = trader(engine, store, gate)
    t.on_run()
    r = t.report()
    assert r["counts"] == {"OPENED": 1} and r["allowed"] == ["MEAN_REVERSION", "MOMENTUM"]
    assert r["enabled"] is True and r["last_run"]["placed"] == 1


def test_app_wiring_is_off_by_default_and_fails_closed(monkeypatch):
    from fastapi.testclient import TestClient
    from jarvis.app import main
    monkeypatch.delenv("JARVIS_PAPER_SIGNALS", raising=False)
    with TestClient(main.app) as c:
        assert main.signal_trader is not None and main.signal_trader.enabled is False
        body = c.get("/api/paper").json()["signal_trading"]
        assert body["enabled"] is False
        assert main.signal_trader.on_run()["placed"] == 0
