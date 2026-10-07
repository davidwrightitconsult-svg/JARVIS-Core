"""
Phase 10A — the Risk Governor.

Principles under test:
- The governor can only refuse or reduce. It never approves more than was
  asked, never approves LIVE, and never approves a strategy the validation
  gate has not allowed. If the gate cannot be read, the answer is no.
- The kill switch and the drawdown halt stop everything, survive a restart,
  and are released only with the literal word RESUME.
- Sizes are capped per position, per market, per strategy and in total, as
  fractions of CURRENT equity; what is left below the minimum is refused.
- Garbage in (NaN, negative, empty) is refused, not sized.
- Every decision is recorded with its checks; a dry run records nothing.
- Settings cannot be configured past the hard ceilings.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from friday.risk import (APPROVED, REDUCED, REJECTED, Portfolio, Position, Proposal, RiskGovernor, RiskSettings,
                         RiskState)

GATE = {"approved_for_paper": ["MOMENTUM"], "forward_test": ["RESEARCH_FORECAST"], "live_approved": []}


@pytest.fixture
def state(tmp_path):
    return RiskState(tmp_path / "risk.db")


def governor(state, gate=GATE, **settings):
    return RiskGovernor(RiskSettings(**settings), state, (lambda: gate) if gate is not None else None)


def ask(stake=10.0, strategy="RESEARCH_FORECAST", market="m1", mode="PAPER"):
    return Proposal(strategy=strategy, market_id=market, stake=stake, mode=mode)


def held(*positions, equity=1000.0, peak=None, day_start=None):
    return Portfolio(equity=equity, peak_equity=peak or equity, day_start_equity=day_start or equity,
                     positions=tuple(Position(*p) for p in positions))


# ================================================================ what it will never do

def test_live_is_never_approved(state):
    d = governor(state).evaluate(ask(mode="LIVE"))
    assert d["verdict"] == REJECTED and d["reason"] == "LIVE_TRADING_LOCKED" and d["approved_stake"] == 0
    assert d["execution_locked"] is True and governor(state).evaluate(ask(mode="live"))["verdict"] == REJECTED
    assert governor(state).evaluate(ask(mode="SHADOW"))["reason"] == "UNKNOWN_MODE"


def test_only_strategies_the_gate_allows(state):
    g = governor(state)
    assert g.evaluate(ask(strategy="MOMENTUM"))["verdict"] == APPROVED               # approved for paper
    assert g.evaluate(ask(strategy="RESEARCH_FORECAST"))["verdict"] == APPROVED      # forward test
    d = g.evaluate(ask(strategy="CALIBRATION_EDGE"))
    assert d["verdict"] == REJECTED and d["reason"] == "NOT_ALLOWED_BY_GATE"
    assert g.evaluate(ask(strategy="MADE_UP"))["reason"] == "NOT_ALLOWED_BY_GATE"


def test_no_gate_no_trade(state):
    assert governor(state, gate=None).evaluate(ask())["reason"] == "GATE_UNAVAILABLE"
    assert governor(state, gate={}).evaluate(ask())["reason"] == "GATE_UNAVAILABLE"

    def broken():
        raise RuntimeError("market store offline")
    d = RiskGovernor(RiskSettings(), state, broken).evaluate(ask())
    assert d["verdict"] == REJECTED and d["reason"] == "GATE_UNAVAILABLE"
    assert "gate error: RuntimeError" in d["checks"][-1]["detail"]


def test_it_never_approves_more_than_was_asked(state):
    g = governor(state)
    for stake in (1.0, 5.0, 19.99, 20.0, 20.01, 500.0):
        d = g.evaluate(ask(stake=stake))
        assert d["approved_stake"] <= stake and d["approved_stake"] <= 20.0          # 2% of 1000


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf")])
def test_garbage_is_refused(state, bad):
    assert governor(state).evaluate(ask(stake=bad))["reason"] == "INVALID_PROPOSAL"


def test_empty_names_and_a_broken_portfolio_are_refused(state):
    g = governor(state)
    assert g.evaluate(ask(market=" "))["reason"] == "INVALID_PROPOSAL"
    assert g.evaluate(Proposal("", "m1", 10))["reason"] == "NOT_ALLOWED_BY_GATE"      # no strategy: not in the gate
    assert g.evaluate(ask(), held(equity=0.0))["reason"] == "INVALID_PROPOSAL"
    assert g.evaluate(ask(), held(("m2", "MOMENTUM", float("nan"))))["reason"] == "INVALID_PROPOSAL"


# ================================================================ kill switch and halts

def test_kill_switch_stops_everything_and_survives_a_restart(state):
    g = governor(state)
    state.engage_kill_switch("something looks wrong", "owner")
    d = g.evaluate(ask())
    assert d["verdict"] == REJECTED and d["reason"] == "KILL_SWITCH"
    again = governor(RiskState(state.path))                                          # "restart"
    assert again.evaluate(ask())["reason"] == "KILL_SWITCH" and again.status()["kill_switch"]["engaged"]
    with pytest.raises(PermissionError):
        state.resume("yes please")
    assert g.evaluate(ask())["reason"] == "KILL_SWITCH"
    assert state.resume("RESUME")["cleared"]["kill_switch"] is True
    assert g.evaluate(ask())["verdict"] == APPROVED


def test_drawdown_halt_persists_until_the_owner_resumes(state):
    g = governor(state)
    assert g.evaluate(ask(), held(equity=905, peak=1000))["verdict"] == APPROVED     # 9.5% down: still allowed
    d = g.evaluate(ask(), held(equity=900, peak=1000))                               # 10% down
    assert d["reason"] == "DRAWDOWN_HALT" and state.drawdown_halt()["engaged"]
    assert g.evaluate(ask(), held(equity=1000, peak=1000))["reason"] == "DRAWDOWN_HALT"   # recovering does not lift it
    assert governor(RiskState(state.path)).evaluate(ask())["reason"] == "DRAWDOWN_HALT"
    state.resume("RESUME")
    assert g.evaluate(ask(), held(equity=1000, peak=1000))["verdict"] == APPROVED


def test_daily_loss_limit_stops_new_positions_for_the_day(state):
    g = governor(state)
    assert g.evaluate(ask(), held(equity=975, peak=1000, day_start=1000))["verdict"] == APPROVED
    d = g.evaluate(ask(), held(equity=970, peak=1000, day_start=1000))               # 3% down today
    assert d["reason"] == "DAILY_LOSS_LIMIT" and not state.drawdown_halt()["engaged"]
    assert g.evaluate(ask(), held(equity=970, peak=1000, day_start=970))["verdict"] == APPROVED   # the next day


# ================================================================ size caps

def test_position_cap_reduces(state):
    d = governor(state).evaluate(ask(stake=50))
    assert d["verdict"] == REDUCED and d["approved_stake"] == 20.0 and "per position" in d["reason"]
    assert governor(state).evaluate(ask(stake=50), held(equity=500))["approved_stake"] == 10.0   # current equity


def test_market_strategy_and_total_caps(state):
    g = governor(state)
    d = g.evaluate(ask(stake=20, market="m1"), held(("m1", "MOMENTUM", 30.0)))        # 4% per market = 40
    assert d["verdict"] == REDUCED and d["approved_stake"] == 10.0 and "per market" in d["reason"]
    mine = [(f"m{i}", "RESEARCH_FORECAST", 20.0) for i in range(2, 9)]                # 140 of the strategy's 150
    d = g.evaluate(ask(stake=20, market="m1"), held(*mine))
    assert d["verdict"] == REDUCED and d["approved_stake"] == 10.0 and "per strategy" in d["reason"]
    full = [(f"m{i}", f"S{i % 3}", 21.0) for i in range(2, 16)]                       # 294 of the total 300
    d = g.evaluate(ask(stake=20, market="m1"), held(*full))
    assert d["verdict"] == REDUCED and d["approved_stake"] == 6.0 and "total exposure" in d["reason"]


def test_no_room_is_refused_not_rounded_up(state):
    g = governor(state)
    d = g.evaluate(ask(stake=20, market="m1"), held(("m1", "MOMENTUM", 39.5)))        # 50c of room < $1 minimum
    assert d["verdict"] == REJECTED and d["reason"] == "NO_ROOM (per market)" and d["approved_stake"] == 0
    assert g.evaluate(ask(stake=0.5))["reason"].startswith("NO_ROOM")                 # asked below the minimum


def test_open_positions_limit(state):
    g = governor(state, max_open_positions=3)
    three = [(f"m{i}", "MOMENTUM", 5.0) for i in (2, 3, 4)]
    assert g.evaluate(ask(market="m9"), held(*three))["reason"] == "MAX_OPEN_POSITIONS"
    assert g.evaluate(ask(market="m2"), held(*three))["verdict"] == APPROVED          # adding to one already held


# ================================================================ settings

def test_settings_cannot_exceed_the_hard_ceilings(monkeypatch):
    for bad in ({"max_position_fraction": 0.2}, {"max_total_exposure": 0.9}, {"max_drawdown": 0.5},
                {"daily_loss_limit": 0.5}, {"max_position_fraction": 0.05, "max_market_fraction": 0.04},
                {"bankroll": 0}, {"max_drawdown": 0}, {"max_open_positions": 0}):
        with pytest.raises(ValueError):
            RiskSettings(**bad)
    monkeypatch.setenv("FRIDAY_RISK_BANKROLL", "5000")
    monkeypatch.setenv("FRIDAY_RISK_MAX_POSITIONS", "8")
    s = RiskSettings.from_environment()
    assert s.bankroll == 5000 and s.max_open_positions == 8 and s.max_position_fraction == 0.02
    monkeypatch.setenv("FRIDAY_RISK_MAX_POSITION", "0.5")
    with pytest.raises(ValueError):
        RiskSettings.from_environment()


# ================================================================ record

def test_every_decision_is_recorded_with_its_checks(state):
    g = RiskGovernor(RiskSettings(), state, lambda: GATE, clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc))
    a = g.evaluate(ask(stake=50))
    b = g.evaluate(ask(strategy="CALIBRATION_EDGE"))
    g.evaluate(ask(), record=False)                                                   # dry run: not recorded
    log = state.decisions()
    assert [d["decision_id"] for d in log] == [b["decision_id"], a["decision_id"]]
    assert log[1]["verdict"] == REDUCED and log[1]["requested_stake"] == 50 and log[1]["at"].startswith("2026-10-03")
    assert {c["check"] for c in log[1]["checks"]} >= {"live trading locked", "kill switch", "validation gate",
                                                      "drawdown halt", "daily loss limit", "cap: per position"}
    assert state.decisions(verdict="REJECTED")[0]["reason"] == "NOT_ALLOWED_BY_GATE"
    assert g.status()["live_trading"] == "LOCKED" and g.status()["gate"]["live_approved"] == []


def test_cli(state, capsys, monkeypatch):
    from friday.risk.__main__ import main as cli
    assert cli(["kill", "--db", str(state.path), "--reason", "test"]) == 0
    assert cli(["resume", "--db", str(state.path), "--confirm", "no"]) == 3
    assert "REFUSED" in capsys.readouterr().out and state.kill_switch()["engaged"]
    assert cli(["resume", "--db", str(state.path), "--confirm", "RESUME"]) == 0 and not state.kill_switch()["engaged"]
    assert cli(["status", "--db", str(state.path)]) == 0
    assert '"live_trading": "LOCKED"' in capsys.readouterr().out
    governor(state).evaluate(ask())
    assert cli(["decisions", "--db", str(state.path)]) == 0 and "APPROVED" in capsys.readouterr().out


# ================================================================ app

def test_app_risk_endpoints(tmp_path, monkeypatch):
    from friday.market.store import MarketStore
    from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
    from friday.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    books = {"yes1": _fresh(raw_book("yes1", [(0.48, 5000)], [(0.50, 5000)])),
             "no1": _fresh(raw_book("no1", [(0.50, 5000)], [(0.52, 5000)]))}
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], books), store)
    with TestClient(main.app) as c:
        status = c.get("/api/risk/status").json()
        assert status["risk_version"] == "10C.1" and status["live_trading"] == "LOCKED"
        assert status["gate"]["forward_test"] == ["RESEARCH_FORECAST", "BOLLINGER_RESTING"] and status["gate"]["approved_for_paper"] == []
        ok = c.post("/api/risk/evaluate", json={"strategy": "RESEARCH_FORECAST", "market_id": "1", "stake": 50}).json()
        assert ok["verdict"] == "REDUCED" and ok["approved_stake"] == 20.0 and "decision_id" not in ok
        no = c.post("/api/risk/evaluate", json={"strategy": "CALIBRATION_EDGE", "market_id": "1", "stake": 5}).json()
        assert no["verdict"] == "REJECTED" and no["reason"] == "NOT_ALLOWED_BY_GATE"
        live = c.post("/api/risk/evaluate", json={"strategy": "RESEARCH_FORECAST", "market_id": "1", "stake": 5,
                                                  "mode": "LIVE"}).json()
        assert live["reason"] == "LIVE_TRADING_LOCKED"
        held_pf = {"equity": 1000, "positions": [{"market_id": "1", "strategy": "X", "stake": 35}]}
        cut = c.post("/api/risk/evaluate", json={"strategy": "RESEARCH_FORECAST", "market_id": "1", "stake": 20,
                                                 "portfolio": held_pf}).json()
        assert cut["approved_stake"] == 5.0
        assert c.post("/api/risk/evaluate", json={"strategy": "x", "stake": "abc"}).status_code == 422
        assert c.post("/api/risk/kill", json={"reason": "drill"}).json()["kill_switch"]["engaged"] is True
        assert c.post("/api/risk/evaluate", json={"strategy": "RESEARCH_FORECAST", "market_id": "1",
                                                  "stake": 5}).json()["reason"] == "KILL_SWITCH"
        assert c.post("/api/risk/resume", json={"confirm": "ok"}).status_code == 403
        assert c.post("/api/risk/resume", json={"confirm": "RESUME"}).json()["resumed"] is True
        assert c.get("/api/risk/decisions").json()["decisions"] == []                    # dry runs record nothing
        assert c.get("/health").json()["execution_locked"] is True
