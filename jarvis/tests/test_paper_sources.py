"""Phase 12B: resting paper orders and the Bollinger forward test."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jarvis.paper import BollingerResting, Order, PaperEngine, PaperSettings
from jarvis.paper import sources
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

NAME = sources.NAME


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 5, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Market:
    """Stands in for the market store: latest rows, snapshot history, quotes, resolutions."""
    def __init__(self, clock):
        self.clock, self.rows, self.hist, self.res, self.watched = clock, {}, {}, {}, []

    def quote(self, market, token, bid, ask, outcome="Yes", order=0):
        row = {"market_id": market, "token_id": token, "best_bid": bid, "best_ask": ask, "mid": None if bid is None else (bid + ask) / 2,
               "usable": True, "observed_at": self.clock().isoformat(timespec="seconds"), "question": f"Q {market}?", "outcome": outcome,
               "outcome_order": order}
        self.rows[token] = row
        self.hist.setdefault(token, []).append(dict(row))

    def past(self, token, mids):
        """Hourly mids for the completed hours before now, oldest first."""
        top = self.clock().replace(minute=30, second=0, microsecond=0)
        self.hist[token] = [{"observed_at": (top - timedelta(hours=len(mids) - i)).isoformat(timespec="seconds"), "mid": m, "usable": True}
                            for i, m in enumerate(mids)]

    def latest_rows(self, market_ids=None):
        return sorted(self.rows.values(), key=lambda r: (r["market_id"], r["outcome_order"]))

    def history_bulk(self, token_ids, hours=25):
        return {t: list(self.hist.get(t, [])) for t in token_ids}

    def quote_near(self, token_id, at, tolerance_hours=3.0):
        r = self.rows.get(token_id)
        if not r:
            return None
        age = datetime.fromisoformat(at) - datetime.fromisoformat(r["observed_at"])
        return r if timedelta(0) <= age <= timedelta(hours=tolerance_hours) else None

    def resolutions(self, market_ids=None):
        return {m: r for m, r in self.res.items() if market_ids is None or m in market_ids}

    def record_resolution(self, market_id, payoffs, source="gamma", now=None):
        self.res[market_id] = {"payoffs": payoffs}

    def watch(self, market_ids, now=None):
        self.watched.extend(market_ids)


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    state = RiskState(tmp_path / "r.db")
    market = Market(clock)
    ledger = PaperLedger(tmp_path / "r.db", 1000.0, clock)
    box = {}
    governor = RiskGovernor(RiskSettings(), state, lambda: {"approved_for_paper": [], "forward_test": [
        e["strategy"] for e in [box["src"].gate_entry()] if e and e["status"] == "FORWARD_TEST"], "live_approved": []}, clock)
    engine = PaperEngine(governor, ledger, market, PaperSettings(), clock=clock)
    box["src"] = BollingerResting(engine, market, clock=clock)
    return engine, box["src"], market, clock, state, ledger


def order(**kw):
    return Order(**{**dict(strategy=NAME, market_id="m1", token_id="yes1", stake=20.0, stop=0.04, target=0.05, max_hold_hours=48, label="q"), **kw})


def test_a_resting_order_fills_only_when_the_market_trades_through_it(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m1", "yes1", 0.48, 0.50)
    r = eng.rest(order(), 0.48)
    assert r["status"] == "RESTING" and ledger.positions("OPEN") == [] and ledger.summary()["cash"] == 1000.0
    assert eng.rest(order(), 0.48)["reason"].startswith("this strategy already holds or is waiting")
    assert eng.tick()["filled"] == []                                           # same quote as at placement: nothing
    clock.advance(minutes=15)
    mk.quote("m1", "yes1", 0.47, 0.48)                                          # the ask TOUCHES 0.48: not a fill
    assert eng.tick()["filled"] == []
    clock.advance(minutes=15)
    mk.quote("m1", "yes1", 0.45, 0.47)                                          # the ask is BELOW 0.48: filled at 0.48
    t = eng.tick()
    assert len(t["filled"]) == 1 and t["filled"][0]["price"] == 0.48
    [pos] = ledger.positions("OPEN")
    assert pos["entry_price"] == 0.48 and pos["stake"] == 20.0 and pos["strategy"] == NAME
    plan = eng._plans()[pos["position_id"]]
    assert plan["stop_price"] == 0.44 and plan["target_price"] == 0.53
    assert ledger.summary(eng.prices())["equity"] < 1000.0                      # it filled because the price fell: already under water
    clock.advance(minutes=15)
    mk.quote("m1", "yes1", 0.54, 0.56)
    assert [c["reason"] for c in eng.tick()["closed"]] == ["target"]
    [trade] = eng.trades()
    assert trade["rested"] is True and trade["per_share"] == 0.05 and trade["entry_spread"] == 0.0
    rep = {r["strategy"]: r for r in eng.report()["strategies"]}[NAME]
    assert rep["resting"]["placed"] == 1 and rep["resting"]["filled"] == 1 and rep["trades"] == 1


def test_resting_orders_expire_and_are_refused_when_they_are_not_resting(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m1", "yes1", 0.48, 0.50)
    assert "below the ask" in eng.rest(order(), 0.50)["reason"]
    assert "outside" in eng.rest(order(), 0.02)["reason"]
    assert eng.rest(order(token_id="none"), 0.48)["status"] == "REFUSED"
    assert eng.rest(order(strategy="BOOK_RSI"), 0.48)["reason"] == "risk governor: NOT_ALLOWED_BY_GATE"
    for bad in (dict(stake=0), dict(stop=float("nan")), dict(target=2)):
        assert eng.rest(order(**bad), 0.48)["status"] == "REFUSED"
    assert eng.rest(order(), 0.48, ttl_minutes=0)["status"] == "REFUSED"
    assert eng.rest(order(), 0.48, ttl_minutes=30)["status"] == "RESTING"
    clock.advance(minutes=45)
    mk.quote("m1", "yes1", 0.49, 0.51)
    t = eng.tick()
    assert t["expired"] == 1 and eng.resting("PENDING") == [] and eng.resting("EXPIRED")[0]["note"] == "not reached in time"
    assert ledger.positions(None) == []


def test_a_fill_still_needs_the_governor(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m1", "yes1", 0.48, 0.50)
    assert eng.rest(order(stake=500), 0.48)["status"] == "RESTING"
    state.engage_kill_switch("stop")
    clock.advance(minutes=15)
    mk.quote("m1", "yes1", 0.44, 0.46)
    t = eng.tick()
    assert t["filled"] == [] and eng.resting("REFUSED")[0]["note"] == "risk governor: KILL_SWITCH"
    assert ledger.positions(None) == []
    state.resume("RESUME")
    mk.quote("m1", "yes1", 0.48, 0.50)
    eng.rest(order(stake=500), 0.48)
    clock.advance(minutes=15)
    mk.quote("m1", "yes1", 0.44, 0.46)
    eng.tick()
    assert ledger.positions("OPEN")[0]["stake"] == 20.0                         # cut to the 2% cap at the fill


def dipped(n=24, low=0.40):
    """Flat at 0.50, one hour far below the lower band, then back inside: the book's LONG signal."""
    return [0.50] * (n - 2) + [low, 0.49]


def test_the_source_places_a_resting_buy_on_the_books_signal_once_an_hour(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m1", "yes1", 0.47, 0.49, "Yes", 0)
    mk.quote("m1", "no1", 0.51, 0.53, "No", 1)
    mk.past("yes1", dipped())
    r = src.on_run()
    assert r["looked"] == 1 and r["signals"] == 1 and r["placed"] == 1
    [o] = eng.resting("PENDING")
    assert o["strategy"] == NAME and o["token_id"] == "yes1" and o["limit_price"] == 0.47
    assert o["stop"] == 0.05 and o["target"] == pytest.approx(0.4945 - 0.47, abs=1e-4)   # 3c + the 2c spread; back to the average
    assert src.on_run()["looked"] == 0 and len(eng.resting("PENDING")) == 1    # same hour: not looked at again
    clock.advance(hours=1)
    mk.quote("m1", "yes1", 0.47, 0.49, "Yes", 0); mk.quote("m1", "no1", 0.51, 0.53, "No", 1)
    mk.past("yes1", dipped()[1:] + [0.49])                                      # the signal hour has passed
    assert src.on_run()["signals"] == 0


def test_a_spike_buys_the_opposite_outcome(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m2", "yes2", 0.50, 0.52, "Yes", 0)
    mk.quote("m2", "no2", 0.48, 0.50, "No", 1)
    mk.past("yes2", [0.50] * 22 + [0.60, 0.51])
    assert src.on_run()["placed"] == 1
    [o] = eng.resting("PENDING")
    assert o["token_id"] == "no2" and o["limit_price"] == 0.48 and "No" in o["label"]


def test_the_source_ignores_thin_history_small_targets_and_one_sided_markets(rig):
    eng, src, mk, clock, state, ledger = rig
    mk.quote("m1", "yes1", 0.47, 0.49, "Yes", 0); mk.quote("m1", "no1", 0.51, 0.53, "No", 1)
    mk.past("yes1", dipped(n=12))
    assert src.on_run()["looked"] == 0                                          # not enough hours
    clock.advance(hours=1)
    mk.quote("m3", "yes3", 0.489, 0.491, "Yes", 0); mk.quote("m3", "no3", 0.509, 0.511, "No", 1)
    mk.past("yes3", dipped())
    mk.quote("m4", "yes4", 0.47, 0.49, "Yes", 0)                                # no second outcome recorded
    mk.past("yes4", dipped())
    mk.rows.pop("yes1"); mk.rows.pop("no1")
    r = src.on_run()
    assert r["looked"] == 1 and r["signals"] == 1 and r["placed"] == 0 and r["refused"] == 1   # under a cent to the average
    assert eng.resting(None) == []


def trade(pnl, per_share, day):
    return {"pnl": pnl, "per_share": per_share, "opened_at": f"2026-10-{day:02d}T10:00:00+00:00", "strategy": NAME}


def test_the_verdict_rule_is_the_one_written_down():
    assert sources.verdict([])["status"] == "RUNNING"
    few = [trade(1.0, 0.02, 1 + i % 5) for i in range(40)]
    assert sources.verdict(few)["status"] == "RUNNING" and "40 closed trades on 5 days" in sources.verdict(few)["reason"]
    good = [trade(0.8, 0.02 + 0.001 * (i % 3), 1 + i % 20) for i in range(120)]
    v = sources.verdict(good)
    assert v["status"] == "SUCCEEDED" and v["interval"][0] > 0 and v["days"] == 20
    bad = [trade(-0.3, -0.01 - 0.001 * (i % 3), 1 + i % 20) for i in range(120)]
    assert sources.verdict(bad)["status"] == "FAILED"
    mixed = [trade(0.1 if i % 2 else -0.1, 0.02 if i % 2 else -0.02, 1 + i % 20) for i in range(120)]
    assert sources.verdict(mixed)["status"] == "RUNNING"
    assert sources.verdict([trade(-10.0, -0.05, 1 + i) for i in range(5)])["status"] == "STOPPED"   # $50 gone
    assert (sources.MIN_TRADES, sources.MIN_DAYS, sources.LOSS_LIMIT, sources.TTL_MINUTES) == (100, 14, 50.0, 60.0)
    assert sources.RULE.params == {"period": 20, "width": 2.0}                  # the book's settings, untouched


def test_a_stopped_or_disabled_test_places_nothing_and_leaves_the_gate(rig, monkeypatch):
    eng, src, mk, clock, state, ledger = rig
    assert src.gate_entry()["status"] == "FORWARD_TEST" and src.gate_entry()["evidence"]["forward"] is True
    mk.quote("m1", "yes1", 0.47, 0.49, "Yes", 0); mk.quote("m1", "no1", 0.51, 0.53, "No", 1)
    mk.past("yes1", dipped())
    monkeypatch.setattr(eng, "trades", lambda limit=500: [trade(-10.0, -0.05, 1 + i) for i in range(6)])
    assert src.standing()["status"] == "STOPPED" and src.on_run().get("inactive") is True
    assert src.gate_entry()["status"] == "NOT_APPROVED" and eng.resting(None) == []
    assert eng.rest(order(), 0.47)["reason"] == "risk governor: NOT_ALLOWED_BY_GATE"      # the governor now refuses it too
    off = BollingerResting(eng, mk, enabled=False, clock=clock)
    assert off.gate_entry() is None and off.on_run().get("inactive") is True


def test_the_gate_lists_forward_tests_but_they_cannot_approve_themselves(tmp_path):
    from jarvis.lab import build_gate
    from jarvis.market.store import MarketStore
    store = MarketStore(tmp_path / "m.db")
    entry = {"strategy": NAME, "status": "FORWARD_TEST", "reason": "r", "evidence": {"forward": True}}
    g = build_gate(store, forward_tests=[entry, {"strategy": "CHEAT", "status": "APPROVED_FOR_PAPER", "reason": "x", "evidence": {}}])
    assert g["forward_test"] == ["RESEARCH_FORECAST", NAME] and g["approved_for_paper"] == [] and "CHEAT" not in str(g["strategies"])
    assert build_gate(store)["forward_test"] == ["RESEARCH_FORECAST"]


def test_app_runs_the_forward_test_after_each_scan(monkeypatch):
    from jarvis.app import main
    with TestClient(main.app) as c:
        assert main.paper_source is not None and main.poly.observers[-2:] == [main.paper_engine.tick, main.paper_source.on_run]
        g = c.get("/api/lab/gate").json()
        assert NAME in g["forward_test"] and g["live_approved"] == []
        p = c.get("/api/paper").json()
        assert p["forward_tests"][0]["strategy"] == NAME and p["forward_tests"][0]["active"] is True and p["resting"] == []
        r = c.post("/api/risk/evaluate", json={"strategy": NAME, "market_id": "m", "stake": 500}).json()
        assert r["verdict"] == "REDUCED" and r["approved_stake"] == 20.0
        assert c.get("/api/dashboard").json()["paper"]["forward_tests"][0]["status"] == "RUNNING"
    monkeypatch.setenv("JARVIS_PAPER_BOLLINGER", "false")
    with TestClient(main.app) as c:
        main._backtest_cache.clear()
        assert NAME not in c.get("/api/lab/gate").json()["forward_test"]
        assert c.post("/api/risk/evaluate", json={"strategy": NAME, "market_id": "m", "stake": 5}).json()["reason"] == "NOT_ALLOWED_BY_GATE"
