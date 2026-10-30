"""Phase 13B: the settlement exit — leave before resolution, and measure whether that was right."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState
from jarvis.tests.test_trigger_agent import FakeWS, connector
from jarvis.trigger import TriggerService, TriggerSettings

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}
YES, NO, MKT = "yes1", "no1", "m1"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    store = MarketStore(tmp_path / "market.db")
    state = RiskState(tmp_path / "risk.db")
    engine = PaperEngine(RiskGovernor(RiskSettings(), state, lambda: GATE, clock),
                         PaperLedger(tmp_path / "risk.db", 1000.0, clock), store, PaperSettings(), clock=clock)
    return store, engine, clock


def seed(store, clock, end_date, bid=0.48, ask=0.50):
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)", (MKT, "Will it?", end_date, at, at))
        db.executemany("INSERT OR REPLACE INTO outcomes(token_id, market_id, name) VALUES(?,?,?)",
                       [(YES, MKT, "Yes"), (NO, MKT, "No")])
    quote(store, clock, bid, ask)


def quote(store, clock, bid, ask):
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                   " spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,?,?,?,?,'[]',1)",
                   (at, YES, MKT, bid, ask, (bid + ask) / 2, ask - bid))


def opened(engine, target=0.30):
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.03, target=target, max_hold_hours=240))
    assert r["status"] == "OPENED", r
    return r


def test_a_position_is_closed_inside_the_settlement_window(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(hours=5)).isoformat())
    opened(engine)
    clock.advance(hours=4)                                   # 60 min to resolution: outside the 30 min window
    quote(store, clock, 0.60, 0.62)
    assert engine.tick()["settled"] == 0

    clock.advance(minutes=45)                                # 15 min to resolution: inside it
    quote(store, clock, 0.60, 0.62)
    summary = engine.tick()
    assert summary["settled"] == 1
    closed = engine.ledger.positions("CLOSED")[0]
    assert closed["close_reason"] == "settlement" and closed["exit_price"] == 0.60


def test_a_position_already_at_the_ceiling_is_left_to_resolve(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(minutes=20)).isoformat())
    opened(engine, target=None)                              # no target, so only settlement could close it
    quote(store, clock, 0.98, 0.99)                          # above settle_hold_above: selling costs more than holding gains
    assert engine.tick()["settled"] == 0 and engine.ledger.positions("OPEN")


def test_settle_hold_above_none_always_exits(rig):
    store, engine, clock = rig
    engine.settings = PaperSettings(settle_hold_above=None)
    seed(store, clock, end_date=(clock() + timedelta(minutes=20)).isoformat())
    opened(engine, target=None)
    quote(store, clock, 0.98, 0.99)
    assert engine.tick()["settled"] == 1


def test_settlement_off_holds_to_resolution(rig):
    store, engine, clock = rig
    engine.settings = PaperSettings(settle_before_minutes=0)
    seed(store, clock, end_date=(clock() + timedelta(minutes=5)).isoformat())
    opened(engine)
    quote(store, clock, 0.60, 0.62)
    assert engine.tick()["settled"] == 0 and engine.ledger.positions("OPEN")


def test_a_market_with_no_end_date_recorded_is_never_settled(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=None)
    opened(engine)
    clock.advance(hours=2)
    quote(store, clock, 0.60, 0.62)
    assert engine.tick()["settled"] == 0


def test_the_stop_still_wins_over_settlement(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(minutes=10)).isoformat())
    opened(engine)                                           # entry 0.50, stop 0.47
    quote(store, clock, 0.40, 0.42)                          # inside the window AND through the stop
    assert engine.tick()["settled"] == 0
    assert engine.ledger.positions("CLOSED")[0]["close_reason"] == "stop"


def test_the_report_measures_whether_exiting_early_was_right(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(minutes=10)).isoformat())
    units = opened(engine)["position"]["units"]
    quote(store, clock, 0.60, 0.62)
    engine.tick()

    before = engine.settlement_report()
    assert before["settled"] == 1 and before["unresolved"] == 1 and before["gain_from_exiting"] is None

    store.record_resolution(MKT, {YES: 1.0, NO: 0.0})        # it would have paid 1.00; we sold at 0.60
    after = engine.settlement_report()
    assert after["resolved"] == 1
    assert after["gain_from_exiting"] == round(units * 0.60 - units * 1.0, 4) < 0
    assert after["detail"][0]["would_have_paid"] == round(units, 4)


def test_the_report_shows_a_gain_when_the_position_would_have_lost(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(minutes=10)).isoformat())
    opened(engine)
    quote(store, clock, 0.60, 0.62)
    engine.tick()
    store.record_resolution(MKT, {YES: 0.0, NO: 1.0})        # it would have paid nothing
    r = engine.settlement_report()
    assert r["gain_from_exiting"] > 0 and r["avg_per_settlement"] == r["gain_from_exiting"]


def test_bad_settlement_settings_are_rejected():
    for bad in (dict(settle_before_minutes=-1), dict(settle_before_minutes=2000), dict(settle_hold_above=0.4),
                dict(settle_hold_above=1.5)):
        with pytest.raises(ValueError):
            PaperSettings(**bad)


def test_the_heartbeat_ticks_the_engine_without_any_price_move(rig):
    store, engine, clock = rig
    seed(store, clock, end_date=(clock() + timedelta(minutes=10)).isoformat())
    opened(engine)
    quote(store, clock, 0.60, 0.62)

    async def t():
        svc = TriggerService(store, engine, TriggerSettings(enabled=True, heartbeat_s=0.05),
                             connect=connector(FakeWS([]), FakeWS([]), FakeWS([])))
        svc.start()
        await asyncio.sleep(0.25)
        await svc.stop()
        return svc
    svc = asyncio.run(t())
    assert svc.beats >= 1                                    # no frames arrived; the heartbeat still ran
    assert engine.ledger.positions("CLOSED")[0]["close_reason"] == "settlement"


def test_heartbeat_can_be_switched_off_and_bad_values_rejected(monkeypatch):
    assert TriggerSettings(heartbeat_s=0).heartbeat_s == 0
    monkeypatch.setenv("JARVIS_TRIGGER", "true")
    monkeypatch.setenv("JARVIS_TRIGGER_HEARTBEAT_S", "2")
    with pytest.raises(ValueError):
        TriggerSettings.from_environment()
    monkeypatch.setenv("JARVIS_TRIGGER_HEARTBEAT_S", "120")
    assert TriggerSettings.from_environment().heartbeat_s == 120
