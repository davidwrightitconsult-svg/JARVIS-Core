"""
Phase 10B — the paper ledger and the read-only dashboard.

Principles under test:
- Paper money only. A position can be opened solely from a RECORDED governor
  decision that is APPROVED or REDUCED in PAPER mode, for exactly the stake
  approved, and each decision opens at most one position.
- Cash never goes below zero; closing realises units x exit price.
- Equity is cash plus open positions at their current price (at cost when
  there is no price); the peak and the day's start are tracked and survive
  a restart; the governor's halts act on those numbers.
- The dashboard is read-only: one GET, every section independent, no
  invented scores, live trading shown as locked.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from friday.risk import LedgerError, PaperLedger, Proposal, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": [], "forward_test": ["RESEARCH_FORECAST"], "live_approved": []}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def kit(tmp_path):
    clock = Clock()
    state = RiskState(tmp_path / "risk.db")
    return RiskGovernor(RiskSettings(), state, lambda: GATE, clock=clock), PaperLedger(state.path, 1000.0, clock), clock


def approve(gov, ledger, stake=20.0, market="m1", strategy="RESEARCH_FORECAST", **kw):
    return gov.evaluate(Proposal(strategy, market, stake, **kw), ledger.portfolio())


# ================================================================ opening

def test_a_position_opens_only_from_a_recorded_approved_paper_decision(kit):
    gov, ledger, _ = kit
    pos = ledger.open(approve(gov, ledger), price=0.40, instrument="tokYES", label="Will X happen?")
    assert (pos["stake"], pos["entry_price"], pos["units"], pos["status"]) == (20.0, 0.40, 50.0, "OPEN")
    assert ledger.summary()["cash"] == 980.0 and ledger.summary()["equity"] == 1000.0     # carried at cost
    with pytest.raises(LedgerError, match="did not approve"):
        ledger.open(approve(gov, ledger, strategy="CALIBRATION_EDGE"), 0.4)
    with pytest.raises(LedgerError, match="did not approve"):
        ledger.open(approve(gov, ledger, mode="LIVE"), 0.4)
    with pytest.raises(LedgerError, match="dry-run"):
        ledger.open(gov.evaluate(Proposal("RESEARCH_FORECAST", "m2", 20.0), ledger.portfolio(), record=False), 0.4)
    with pytest.raises(LedgerError, match="paper positions only"):
        ledger.open({"verdict": "APPROVED", "mode": "LIVE", "decision_id": 99, "approved_stake": 5}, 0.4)
    assert len(ledger.positions()) == 1


def test_the_stake_is_what_the_governor_approved_and_a_decision_is_used_once(kit):
    gov, ledger, _ = kit
    d = approve(gov, ledger, stake=500.0)                                        # cut to the 2% cap
    assert d["verdict"] == "REDUCED" and ledger.open(d, 0.5)["stake"] == 20.0
    with pytest.raises(LedgerError, match="already been used"):
        ledger.open(d, 0.5)


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_bad_prices_are_refused(kit, price):
    gov, ledger, _ = kit
    with pytest.raises(LedgerError):
        ledger.open(approve(gov, ledger), price)


def test_cash_never_goes_below_zero(tmp_path):
    state = RiskState(tmp_path / "risk.db")
    ledger = PaperLedger(state.path, 10.0)
    with pytest.raises(LedgerError, match="not enough paper cash"):
        ledger.open({"verdict": "APPROVED", "mode": "PAPER", "decision_id": 1, "approved_stake": 25.0,
                     "strategy": "S", "market_id": "m"}, 0.5)
    assert ledger.summary()["cash"] == 10.0
    with pytest.raises(ValueError):
        PaperLedger(tmp_path / "x.db", 0)


# ================================================================ valuing and closing

def test_mark_to_market_close_and_summary(kit):
    gov, ledger, _ = kit
    win = ledger.open(approve(gov, ledger, market="m1"), 0.40)                   # 50 units
    lose = ledger.open(approve(gov, ledger, market="m2"), 0.50)                  # 40 units
    pf = ledger.portfolio({win["position_id"]: 0.60, lose["position_id"]: 0.25})
    assert pf.equity == pytest.approx(960 + 30 + 10) and pf.exposure == 40.0 and len(pf.positions) == 2
    assert ledger.close(win["position_id"], 1.0, "resolved YES")["pnl"] == 30.0
    assert ledger.close(lose["position_id"], 0.0, "resolved NO")["pnl"] == -20.0
    with pytest.raises(LedgerError):
        ledger.close(win["position_id"], 1.0)
    s = ledger.summary()
    assert (s["equity"], s["cash"], s["total_pnl"], s["open_positions"], s["closed_trades"]) == (1010.0, 1010.0, 10.0, 0, 2)
    assert (s["win_rate"], s["best_trade"], s["worst_trade"], s["mode"]) == (0.5, 30.0, -20.0, "PAPER")


def test_peak_day_start_and_restart(kit):
    gov, ledger, clock = kit
    pos = ledger.open(approve(gov, ledger), 0.50)                                # 40 units
    assert ledger.portfolio({pos["position_id"]: 1.0}).peak_equity == 1020.0     # peak follows equity up
    pf = ledger.portfolio({pos["position_id"]: 0.25})
    assert pf.equity == 990.0 and pf.peak_equity == 1020.0 and pf.day_start_equity == 1000.0
    clock.now += timedelta(days=1)                                               # next UTC day
    assert ledger.portfolio({pos["position_id"]: 0.25}).day_start_equity == 990.0
    again = PaperLedger(ledger.path, 5000.0, clock)                              # restart: the bankroll is not reset
    pf = again.portfolio({pos["position_id"]: 0.25})
    assert pf.equity == 990.0 and pf.peak_equity == 1020.0 and again.summary()["bankroll"] == 1000.0
    assert [d["day"] for d in again.summary()["equity_days"]] == ["2026-10-03", "2026-10-04"]


def test_the_governor_s_halts_act_on_the_ledger(kit):
    gov, ledger, _ = kit
    opened = [ledger.open(approve(gov, ledger, market=f"m{i}"), 0.50) for i in range(6)]      # $120 at risk
    assert approve(gov, ledger, market="m9")["verdict"] == "APPROVED"
    crash = {p["position_id"]: 0.0 for p in opened}                              # all six go to zero: -12%
    d = gov.evaluate(Proposal("RESEARCH_FORECAST", "m9", 20.0), ledger.portfolio(crash))
    assert d["verdict"] == "REJECTED" and d["reason"] == "DRAWDOWN_HALT"
    assert gov.status(ledger.portfolio(crash))["portfolio"]["open_markets"] == 6


# ================================================================ dashboard

def _client(tmp_path, monkeypatch):
    from friday.market.store import MarketStore
    from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
    from friday.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    books = {"yes1": _fresh(raw_book("yes1", [(0.48, 5000)], [(0.50, 5000)])),
             "no1": _fresh(raw_book("no1", [(0.50, 5000)], [(0.52, 5000)]))}
    return _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], books), store)


def test_portfolio_and_dashboard_data(tmp_path, monkeypatch):
    main = _client(tmp_path, monkeypatch)
    with TestClient(main.app) as c:
        p = c.get("/api/portfolio").json()
        assert p["mode"] == "PAPER" and p["equity"] == 1000.0 and p["positions"] == [] and p["live_trading"] == "LOCKED"
        c.get("/api/scan")                                                      # the market scan records observations
        d = c.get("/api/dashboard").json()
        assert set(d) >= {"system", "scanner", "gate", "risk", "portfolio", "activity", "library", "book_strategies"}
        assert d["live_trading"] == "LOCKED" and d["execution_locked"] is True
        assert d["scanner"]["venue"] == "Polymarket" and d["scanner"]["rows"][0]["question"]
        assert d["gate"]["approved_for_paper"] == [] and d["gate"]["live_approved"] == []
        assert d["portfolio"]["equity"] == 1000.0 and d["risk"]["portfolio"]["equity"] == 1000.0
        assert d["book_strategies"]["tested"] == 0 and len(d["book_strategies"]["names"]) == 12
        assert "error" in d["library"]                                          # no library in tests: said, not hidden
        # a real paper position shows up, valued at FRIDAY's own recorded bid
        dec = main.risk_governor.evaluate(Proposal("RESEARCH_FORECAST", "1", 20.0), main.paper_ledger.portfolio())
        main.paper_ledger.open(dec, 0.50, instrument="yes1", label="Will event 1 happen?")
        d = c.get("/api/dashboard").json()
        [pos] = d["portfolio"]["positions"]
        assert pos["price"] == 0.48 and pos["value"] == 19.2 and d["portfolio"]["equity"] == 999.2
        assert any(a["type"] == "Risk check" and a["result"] == "APPROVED" for a in d["activity"])


def test_one_broken_section_does_not_take_the_dashboard_down(tmp_path, monkeypatch):
    main = _client(tmp_path, monkeypatch)
    with TestClient(main.app) as c:
        monkeypatch.setattr(main, "paper_ledger", None)
        c.get("/api/scan")
        d = c.get("/api/dashboard").json()
        assert "error" in d["portfolio"] and d["scanner"]["rows"] and d["gate"]["gate_version"]
        assert c.get("/api/portfolio").status_code == 503


def test_the_page_is_read_only_and_makes_no_claims(tmp_path, monkeypatch):
    import pathlib
    static = pathlib.Path(__file__).resolve().parents[1] / "static"
    js, html = (static / "dashboard.js").read_text(), (static / "dashboard.html").read_text()
    assert js.count("fetch(") == 1 and 'fetch("/api/dashboard"' in js and "POST" not in js and "method:" not in js
    assert "innerHTML" not in js                                                # market text is never parsed as markup
    assert "<form" not in html and "<button" not in html and "<input" not in html
    assert not any(fake in html + js for fake in ("98%", "99%", "$3.30M", "BULLISH", "J.A.R.V.I.S"))
    assert "http://" not in html and "https://" not in html                     # no external assets
    main = _client(tmp_path, monkeypatch)
    with TestClient(main.app) as c:
        assert "Validation board" in c.get("/").text and "Live locked" in c.get("/").text
        assert "COMMAND RESPONSE" in c.get("/console").text
        assert c.get("/static/dashboard.css").status_code == 200 and c.get("/static/dashboard.js").status_code == 200


def test_dashboard_never_calls_the_venue(tmp_path, monkeypatch):
    """The dashboard ranks recorded observations only, even when there are none or they are stale."""
    main = _client(tmp_path, monkeypatch)
    with TestClient(main.app) as c:
        calls = []
        monkeypatch.setattr(main.poly, "snapshot", lambda *a, **k: calls.append(a))
        d = c.get("/api/dashboard").json()
        assert calls == [] and d["scanner"]["rows"] == [] and d["scanner"]["observed_at"] is None
        c.get("/api/scan")
        assert len(calls) == 1                                                  # the scan endpoint still refreshes
