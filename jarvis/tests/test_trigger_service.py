"""Phase 13A: TRIGGER joined to the real paper engine, ledger, governor and market store."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.risk import PaperLedger, Proposal, RiskGovernor, RiskSettings, RiskState
from jarvis.tests.test_trigger_agent import FakeWS, book, connector, pc
from jarvis.trigger import LiveQuotes, TriggerConfig, TriggerService, TriggerSettings

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}
YES, NO, MKT = "yes1", "no1", "m1"


def iso(minutes_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


@pytest.fixture
def rig(tmp_path):
    store = MarketStore(tmp_path / "market.db")
    with store._db() as db:
        db.execute("INSERT INTO markets(market_id, question, first_seen, last_seen) VALUES(?,?,?,?)",
                   (MKT, "Will it?", iso(), iso()))
        db.executemany("INSERT INTO outcomes(token_id, market_id, name) VALUES(?,?,?)",
                       [(YES, MKT, "Yes"), (NO, MKT, "No")])
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (iso(5),))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                         " spread, flags, usable) VALUES(1,?,?,?,0.48,0.50,0.49,0.02,'[]',1)", (iso(5), YES, MKT))
        # 14A: entries need a recorded book. Deep levels keep these tests about the trigger.
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)",
                       [(cur.lastrowid, "bid", 0, 0.48, 2000.0, iso(5)), (cur.lastrowid, "ask", 0, 0.50, 2000.0, iso(5))])
    store.watch([MKT])
    state = RiskState(tmp_path / "risk.db")
    governor = RiskGovernor(RiskSettings(), state, lambda: GATE)
    ledger = PaperLedger(tmp_path / "risk.db", 1000.0)
    engine = PaperEngine(governor, ledger, store, PaperSettings())
    return store, engine, ledger


def open_position(engine):
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.03, target=0.06, max_hold_hours=48))
    assert r["status"] == "OPENED", r
    return r


def settings(**kw):
    cfg = TriggerConfig(move_threshold=0.02, cooldown_s=0.0, priority_move_threshold=0.005, priority_cooldown_s=0.0,
                        ping_interval_s=9, stale_after_s=999, universe_refresh_s=999, backoff_initial_s=0.01)
    return TriggerSettings(enabled=True, min_tick_interval_s=0.0, config=cfg, **kw)


async def run_service(svc, seconds=0.5):
    svc.start()
    await asyncio.sleep(seconds)
    await svc.stop()


def test_a_live_quote_through_the_stop_closes_the_position_without_waiting_for_a_scan(rig):
    store, engine, ledger = rig
    open_position(engine)                                    # entry 0.50, stop 0.47
    assert engine.tick()["closed"] == []                     # recorded quote: nothing to do

    async def t():
        svc = TriggerService(store, engine, settings(), connect=connector(
            FakeWS([book(YES, .48, .50), pc(YES, .45, .47)])))
        await run_service(svc)
        return svc
    svc = asyncio.run(t())
    closed = ledger.positions("CLOSED")
    assert len(closed) == 1 and closed[0]["close_reason"] == "stop" and closed[0]["exit_price"] == 0.45
    assert svc.ticks >= 2 and engine.live_quotes is None     # unhooked again on stop


def test_universe_puts_held_tokens_first_and_respects_the_cap(rig):
    store, engine, _ = rig
    with store._db() as db:
        db.executemany("INSERT INTO outcomes(token_id, market_id, name) VALUES(?,?,?)",
                       [(f"t{i}", MKT, "x") for i in range(30)])
    open_position(engine)
    svc = TriggerService(store, engine, TriggerSettings(enabled=True, max_tokens=10))
    uni = asyncio.run(svc.universe())
    assert uni[0] == YES and len(uni) == 10 and len(set(uni)) == 10


def test_exits_still_work_when_the_platform_is_terminated(rig):
    store, engine, ledger = rig
    open_position(engine)

    class Dead:
        def is_terminated(self): return True
        def snapshot(self): return {"reason": "test"}

    async def t():
        svc = TriggerService(store, engine, settings(), connect=connector(FakeWS([book(YES, .48, .50), pc(YES, .40, .42)])))
        with mock.patch("jarvis.directives.get_terminator", lambda: Dead()):
            await run_service(svc)
    asyncio.run(t())
    assert ledger.positions("CLOSED")[0]["close_reason"] == "stop"


def test_a_resolution_from_the_feed_settles_the_position_at_its_payoff(rig):
    store, engine, ledger = rig
    open_position(engine)
    ev = {"event_type": "market_resolved", "id": "1", "market": "0xM", "assets_ids": [YES, NO],
          "winning_asset_id": YES, "winning_outcome": "Yes", "timestamp": "1"}

    async def t():
        svc = TriggerService(store, engine, settings(), connect=connector(FakeWS([book(YES, .48, .50), ev])))
        await run_service(svc)
    asyncio.run(t())
    assert store.resolutions([MKT])[MKT]["payoffs"] == {YES: 1.0, NO: 0.0}
    assert store.resolutions([MKT])[MKT]["source"] == "ws"
    closed = ledger.positions("CLOSED")
    assert closed[0]["close_reason"] == "resolved" and closed[0]["exit_price"] == 1.0


def test_a_resolution_for_an_unknown_token_is_ignored(rig):
    store, engine, _ = rig

    async def t():
        svc = TriggerService(store, engine, settings(), connect=connector(FakeWS([])))
        await svc._on_resolved({"assets_ids": ["zzz", "yyy"], "winning_asset_id": "zzz"})
        await svc._on_resolved({"assets_ids": ["a"], "winning_asset_id": "not-in-list"})
    asyncio.run(t())
    assert store.resolutions() == {}


def test_a_watchlist_move_is_counted_and_never_trades(rig):
    store, engine, ledger = rig

    async def t():
        svc = TriggerService(store, engine, settings(), connect=connector(FakeWS([pc(YES, .50, .52), pc(YES, .60, .62)])))
        await run_service(svc)
        return svc
    svc = asyncio.run(t())
    assert svc.watch_moves == 1 and svc.ticks == 0 and ledger.positions("OPEN") == []


# ---- the engine's live-quote overlay -------------------------------------------------

def test_overlay_only_wins_when_fresher_and_sane(rig):
    store, engine, _ = rig                                   # recorded: bid .48 ask .50, 5 minutes old
    live = {}
    engine.live_quotes = lambda t: live.get(t)
    assert engine._quote(YES, 15)["ask"] == 0.50
    live[YES] = {"bid": .60, "ask": .62, "at": iso(1)}
    assert engine._quote(YES, 15)["ask"] == 0.62             # fresher: wins
    live[YES] = {"bid": .60, "ask": .62, "at": iso(30)}
    assert engine._quote(YES, 15)["ask"] == 0.50             # too old for the freshness rule: ignored
    live[YES] = {"bid": .70, "ask": .60, "at": iso(0)}
    assert engine._quote(YES, 15)["ask"] == 0.50             # crossed: ignored
    live[YES] = {"bid": 1.4, "ask": 1.5, "at": iso(0)}
    assert engine._quote(YES, 15)["ask"] == 0.50             # not a probability: ignored
    live[YES] = {"bid": .6, "ask": .62, "at": iso(0)}
    engine.live_quotes = lambda t: 1 / 0
    assert engine._quote(YES, 15)["ask"] == 0.50             # a broken feed falls back to the record
    engine.live_quotes = None
    assert engine._quote(YES, 15)["ask"] == 0.50


def test_overlay_serves_a_token_with_no_recorded_quote(rig):
    _, engine, _ = rig
    engine.live_quotes = LiveQuotes().get
    assert engine._quote("never-scanned", 15) is None
    q = LiveQuotes()
    q.set("never-scanned", "", .40, .42, datetime.now(timezone.utc).timestamp())
    engine.live_quotes = q.get
    assert engine._quote("never-scanned", 15)["bid"] == 0.40


def test_live_quotes_cache_is_bounded():
    q = LiveQuotes()
    for i in range(5200):
        q.set(f"t{i}", "", .4, .5, float(i))
    assert len(q) <= 5000 and q.get("t5199") is not None and q.get("t0") is None


# ---- the governor fix found while building this ----------------------------------------

def test_a_terminated_platform_refuses_cleanly_instead_of_raising(tmp_path):
    gov = RiskGovernor(RiskSettings(), RiskState(tmp_path / "r.db"), lambda: GATE)
    ledger = PaperLedger(tmp_path / "r.db", 1000.0)

    class Dead:
        def is_terminated(self): return True
        def snapshot(self): return {"reason": "test"}
    with mock.patch("jarvis.directives.get_terminator", lambda: Dead()):
        decision, position = ledger.open_checked(gov, Proposal("S", "m", 10.0), 0.5)
    assert position is None and decision["verdict"] == "REJECTED" and decision["reason"].startswith("TERMINATED")


def test_settings_from_environment(monkeypatch):
    assert TriggerSettings.from_environment().enabled is False
    monkeypatch.setenv("JARVIS_TRIGGER", "true")
    monkeypatch.setenv("JARVIS_TRIGGER_MAX_TOKENS", "40")
    s = TriggerSettings.from_environment()
    assert s.enabled and s.max_tokens == 40
    monkeypatch.setenv("JARVIS_TRIGGER_MAX_TOKENS", "5")
    with pytest.raises(ValueError):
        TriggerSettings.from_environment()


# ---- app wiring ---------------------------------------------------------------------------

def test_app_wiring_is_off_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    from jarvis.app import main
    monkeypatch.delenv("JARVIS_TRIGGER", raising=False)
    with TestClient(main.app) as c:
        s = c.get("/api/trigger/status").json()
        assert s["enabled"] is False and main.trigger_service is None
        assert main.paper_engine.live_quotes is None
        assert c.post("/api/trigger/status", json={}).status_code == 405


def test_app_wiring_when_enabled_hooks_the_engine_and_unhooks_on_shutdown(monkeypatch):
    from fastapi.testclient import TestClient
    from jarvis.app import main
    monkeypatch.setenv("JARVIS_TRIGGER", "true")
    monkeypatch.setattr("jarvis.trigger.agent._default_connect", lambda url: FakeWS([]))   # never touch the network
    with TestClient(main.app) as c:
        s = c.get("/api/trigger/status").json()
        assert s["enabled"] is True and s["endpoint"].startswith("wss://ws-subscriptions-clob.polymarket.com")
        assert main.paper_engine.live_quotes is not None
        assert c.get("/health").json()["live_execution"] is False
    assert main.paper_engine.live_quotes is None


def test_a_bad_trigger_setting_leaves_it_off_and_says_why(monkeypatch):
    from fastapi.testclient import TestClient
    from jarvis.app import main
    monkeypatch.setenv("JARVIS_TRIGGER", "true")
    monkeypatch.setenv("JARVIS_TRIGGER_MOVE", "0.001")          # below the priority threshold: invalid
    with TestClient(main.app) as c:
        s = c.get("/api/trigger/status").json()
        assert s["enabled"] is False and "thresholds" in s["reason"]
