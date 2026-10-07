"""Phase 12A: the paper trading engine - real recorded quotes in, governed paper positions out."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": ["S"], "forward_test": ["F"], "live_approved": []}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Quotes:
    """Stands in for the market store: FRIDAY's recorded quotes, resolutions and watchlist."""
    def __init__(self, clock):
        self.clock, self.q, self.res, self.watched = clock, {}, {}, []

    def set(self, token, bid, ask, age_minutes=0.0):
        self.q[token] = {"best_bid": bid, "best_ask": ask, "mid": None,
                         "observed_at": (self.clock() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")}

    def quote_near(self, token_id, at, tolerance_hours=3.0):
        q = self.q.get(token_id)
        if not q:
            return None
        age = datetime.fromisoformat(at) - datetime.fromisoformat(q["observed_at"])
        return q if timedelta(0) <= age <= timedelta(hours=tolerance_hours) else None

    def resolutions(self, market_ids=None):
        return {m: r for m, r in self.res.items() if market_ids is None or m in market_ids}

    def record_resolution(self, market_id, payoffs, source="gamma", now=None):
        self.res[market_id] = {"payoffs": payoffs, "detected_at": "x", "source": source}

    def watch(self, market_ids, now=None):
        self.watched.extend(market_ids)


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    state = RiskState(tmp_path / "r.db")
    governor = RiskGovernor(RiskSettings(), state, lambda: GATE, clock)
    ledger = PaperLedger(tmp_path / "r.db", 1000.0, clock)
    quotes = Quotes(clock)
    return PaperEngine(governor, ledger, quotes, PaperSettings(), clock=clock), quotes, clock, state, ledger


def order(**kw):
    base = dict(strategy="S", market_id="m1", token_id="yes1", stake=500.0, stop=0.03, target=0.06, max_hold_hours=48, label="q?")
    return Order(**{**base, **kw})


def test_entry_is_at_the_recorded_ask_for_the_governors_stake(rig):
    eng, q, clock, state, ledger = rig
    q.set("yes1", 0.48, 0.50)
    r = eng.submit(order())
    assert r["status"] == "OPENED" and r["decision"]["verdict"] == "REDUCED"
    pos = r["position"]
    assert pos["entry_price"] == 0.50 and pos["stake"] == 20.0 and pos["units"] == 40.0 and pos["instrument"] == "yes1"
    assert q.watched == ["m1"]                                                    # the market stays recorded while held
    s = ledger.summary(eng.prices())
    assert s["equity"] == 999.2 and s["open_positions"] == 1                     # valued at the bid: the spread is a real cost
    assert eng.submit(order())["reason"] == "this strategy already holds this outcome"
    assert eng.submit(order(strategy="F", token_id="yes1"))["status"] == "OPENED"     # another strategy may


@pytest.mark.parametrize("bid,ask,age,why", [
    (None, None, 0, "no ask recorded"), (0.48, 0.50, 20, "no ask recorded"), (0.40, 0.50, 0, "wider than"),
    (None, 0.50, 0, "no bid recorded"), (0.95, 0.97, 0, "outside"), (0.01, 0.03, 0, "outside")])
def test_no_trade_without_a_fresh_tight_quote(rig, bid, ask, age, why):
    eng, q, *_ = rig
    if ask is not None:
        q.set("yes1", bid, ask, age)
    r = eng.submit(order())
    assert r["status"] == "REFUSED" and why in r["reason"] and r["position"] is None


def test_governor_gate_kill_switch_and_bad_orders_stop_entries_and_are_recorded(rig):
    eng, q, clock, state, ledger = rig
    q.set("yes1", 0.48, 0.50)
    assert "NOT_ALLOWED_BY_GATE" in eng.submit(order(strategy="BOOK_RSI"))["reason"]
    state.engage_kill_switch("test")
    assert "KILL_SWITCH" in eng.submit(order())["reason"]
    state.resume("RESUME")
    for bad in (dict(stake=float("nan")), dict(stake=-5), dict(stop=0), dict(stop=1.5), dict(target=-0.1),
                dict(max_hold_hours=0), dict(token_id=""), dict(strategy=" ")):
        assert eng.submit(order(**bad))["status"] == "REFUSED"
    assert ledger.positions("OPEN") == []
    rep = eng.report()
    assert sum(r["n"] for r in rep["refused"]) == 10 and rep["open"] == 0 and rep["live_trading"] == "LOCKED"


def test_exits_stop_target_time_and_resolution(rig):
    eng, q, clock, state, ledger = rig
    for i, tok in enumerate(("a", "b", "c", "d", "e")):
        q.set(tok, 0.49, 0.50)
        assert eng.submit(order(market_id=f"m{i}", token_id=tok, stake=10, max_hold_hours=24 if tok == "c" else 48))["status"] == "OPENED"
    assert eng.tick()["closed"] == []                                             # nothing has happened yet
    clock.advance(minutes=30)
    q.set("a", 0.44, 0.46)                                                        # through the stop at 0.47: sold at the bid
    q.set("b", 0.58, 0.60)                                                        # past the target at 0.56: sold AT the target
    q.set("c", 0.50, 0.51); q.set("d", 0.50, 0.51); q.set("e", 0.50, 0.51)
    t = eng.tick()
    assert {(c["reason"]) for c in t["closed"]} == {"stop", "target"} and t["open"] == 5
    trades = {x["instrument"]: x for x in eng.trades()}
    assert trades["a"]["exit_price"] == 0.44 and trades["a"]["per_share"] == -0.06 and trades["a"]["r"] == -2.0
    assert trades["b"]["exit_price"] == 0.56 and trades["b"]["r"] == 2.0 and trades["b"]["entry_spread"] == 0.01
    clock.advance(hours=25)
    q.set("c", 0.51, 0.52); q.set("d", 0.51, 0.52)
    assert [c["reason"] for c in eng.tick()["closed"]] == ["time"]                # c's 24 hours are up; d has 48
    q.res["m3"] = {"payoffs": {"d": 1.0, "other": 0.0}}
    t = eng.tick()
    assert [c["reason"] for c in t["closed"]] == ["resolved"] and t["waiting_for_a_price"] == 1   # e has no fresh quote
    trades = {x["instrument"]: x for x in eng.trades()}
    assert trades["d"]["exit_price"] == 1.0 and trades["d"]["pnl"] == 10.0        # 20 shares at 0.50 pay $1 each
    assert [p["instrument"] for p in ledger.positions("OPEN")] == ["e"]           # never closed at an invented price
    clock.advance(days=30)
    assert eng.tick()["waiting_for_a_price"] == 1 and len(ledger.positions("OPEN")) == 1


def test_a_position_can_always_be_closed_and_the_resolver_is_asked_when_prices_stop(tmp_path):
    clock = Clock()
    state = RiskState(tmp_path / "r.db")
    ledger = PaperLedger(tmp_path / "r.db", 1000.0, clock)
    q = Quotes(clock)
    asked = []

    def resolver(ids):
        asked.append(list(ids))
        return [{"market_id": "m1", "resolved": True, "payoffs": {"yes1": 0.0, "no1": 1.0}}]
    eng = PaperEngine(RiskGovernor(RiskSettings(), state, lambda: GATE, clock), ledger, q, resolver=resolver, clock=clock)
    q.set("yes1", 0.49, 0.50)
    assert eng.submit(order(stake=10))["status"] == "OPENED"
    state.engage_kill_switch("panic")
    clock.advance(hours=3)                                                        # the venue stopped quoting
    t = eng.tick()
    assert t["waiting_for_a_price"] == 1 and asked == [["m1"]] and t["closed"] == []
    t = eng.tick()                                                                # the answer is used on the next look
    assert [c["reason"] for c in t["closed"]] == ["resolved"] and t["closed"][0]["pnl"] == -10.0
    assert ledger.positions("OPEN") == [] and state.kill_switch()["engaged"]      # closed with the kill switch still on
    eng2 = PaperEngine(RiskGovernor(RiskSettings(), state, lambda: GATE, clock), ledger, q, clock=clock)
    assert eng2.report()["closed"] == 1                                           # the record survives a restart


def test_report_by_strategy_measures_the_real_spread(rig):
    eng, q, clock, state, ledger = rig
    for i in range(6):
        tok = f"t{i}"
        q.set(tok, 0.48, 0.50)
        eng.submit(order(strategy="S" if i % 2 else "F", market_id=f"m{i}", token_id=tok, stake=10))
        clock.advance(minutes=5)
        q.set(tok, 0.57 if i < 4 else 0.40, 0.59 if i < 4 else 0.42)
        eng.tick()
    rep = eng.report()
    rows = {r["strategy"]: r for r in rep["strategies"]}
    assert rows["F"]["trades"] == 3 and rows["S"]["trades"] == 3 and rep["closed"] == 6 and rep["open"] == 0
    assert rows["F"]["avg_entry_spread"] == 0.02 and rows["F"]["exits"] == {"target": 2, "stop": 1}
    assert rows["S"]["wins"] == 2 and rows["S"]["pnl"] == round(2 * 20 * 0.06 - 20 * 0.10, 2)
    assert rep["last_tick"]["closed"][0]["reason"] == "stop"
    with pytest.raises(ValueError):
        PaperSettings(max_spread=0.5)


def test_app_wiring_is_read_only(tmp_path, monkeypatch):
    from jarvis.app import main
    with TestClient(main.app) as c:
        assert main.paper_engine is not None and main.paper_engine.tick in main.poly.observers
        p = c.get("/api/paper").json()
        assert p["paper_version"] == "12C.1" and p["mode"] == "PAPER" and p["positions"] == [] and p["trades"] == []
        assert p["live_trading"] == "LOCKED" and c.post("/api/paper", json={}).status_code == 405
        d = c.get("/api/dashboard").json()
        assert d["paper"]["open"] == 0 and d["system"]["components"]["paper"] == "READY"
        r = main.paper_engine.submit(Order("RESEARCH_FORECAST", "m", "nope", 10, 0.03, 0.06, 24))
        assert r["status"] == "REFUSED" and "no ask recorded" in r["reason"]      # no recorded quote, no trade
        assert main._paper_prices() == {}
    source = open(main.__file__).read()
    assert "paper_engine.submit(" not in source and "paper_ledger.open" not in source and "paper_ledger.close" not in source
