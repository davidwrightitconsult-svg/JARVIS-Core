"""Phase 10C: stress drills, and the three weaknesses they exposed (now fixed)."""

from friday.risk import (PaperLedger, Portfolio, Proposal, RiskGovernor, RiskSettings, RiskState, render_drills,
                         run_drills)
from friday.risk import drills
from friday.risk.__main__ import main as cli

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}


def _rig(tmp_path, gate=lambda: GATE):
    state = RiskState(tmp_path / "r.db")
    return RiskGovernor(RiskSettings(), state, gate), PaperLedger(tmp_path / "r.db", 1000.0), state


def test_every_drill_passes_on_the_default_limits():
    report = run_drills()
    assert report["passed"], render_drills(report)
    assert len(report["results"]) == 7 and report["store"] == "temporary" and report["execution_locked"] is True
    b = report["bounds"]
    assert b["worst_instant_loss"] <= 0.30 + 1e-6 and b["worst_day_without_open_positions"] <= 0.05 + 1e-6
    assert b["slow_bleed_stopped_at"] <= 0.15 + 1e-6
    assert "RISK DRILLS: PASS" in render_drills(report)


def test_drills_hold_on_other_legal_settings(tmp_path):
    tight = RiskSettings(bankroll=250, max_position_fraction=0.01, max_market_fraction=0.01, max_strategy_fraction=0.05,
                         max_total_exposure=0.05, max_open_positions=3, daily_loss_limit=0.01, max_drawdown=0.03)
    loose = RiskSettings(bankroll=50_000, max_position_fraction=0.10, max_market_fraction=0.20, max_strategy_fraction=0.60,
                         max_total_exposure=0.60, max_open_positions=40, daily_loss_limit=0.10, max_drawdown=0.25)
    for i, s in enumerate((tight, loose)):
        folder = tmp_path / str(i)
        folder.mkdir()
        for d in (drills.storm(str(folder), s, steps=600, seed=i), drills.wipeout(str(folder), s),
                  drills.bleed(str(folder), s), drills.rush(str(folder), s, threads=12)):
            assert d["passed"], d


def test_a_failing_drill_is_reported_not_hidden(monkeypatch):
    def broken(folder, settings):
        raise RuntimeError("boom")
    monkeypatch.setattr(drills, "DRILLS", (broken,))
    report = run_drills()
    assert report["passed"] is False and "crashed" in report["results"][0]["detail"]
    assert "RISK DRILLS: FAIL" in render_drills(report)


def test_the_stake_is_never_rounded_up(tmp_path):
    g, _, _ = _rig(tmp_path)
    d = g.evaluate(Proposal("S", "m", 4.336), Portfolio.fresh(1000))
    assert d["approved_stake"] == 4.33 and d["approved_stake"] <= d["requested_stake"]
    d = g.evaluate(Proposal("S", "m", 500), Portfolio.fresh(999.999))           # cap is 19.99998
    assert d["approved_stake"] == 19.99


def test_gate_names_must_be_a_real_list(tmp_path):
    g, _, _ = _rig(tmp_path, lambda: {"approved_for_paper": "MOMENTUM MEAN_REVERSION"})
    for name in ("MOMENTUM", "MEAN", "M"):                                       # a substring is not an approval
        assert g.evaluate(Proposal(name, "m", 10))["verdict"] == "REJECTED"
    g, _, _ = _rig(tmp_path, lambda: ["S"])
    assert g.evaluate(Proposal("S", "m", 10))["reason"] == "GATE_UNAVAILABLE"


def test_resume_after_a_drawdown_halt_restarts_the_peak(tmp_path):
    g, ledger, state = _rig(tmp_path)
    for i in range(6):                                                           # six total losses: -12%
        d = g.evaluate(Proposal("S", f"m{i}", 20), Portfolio.fresh(1000))
        ledger.close(ledger.open(d, 0.5)["position_id"], 0.0)
    assert g.evaluate(Proposal("S", "x", 10), ledger.portfolio())["reason"] == "DRAWDOWN_HALT"
    assert ledger.portfolio().peak_equity == 1000.0                             # a halt alone does not move the peak
    state.resume("RESUME")
    pf = ledger.portfolio()
    assert pf.peak_equity == pf.equity == 880.0                                 # drawdown is measured from here on
    assert g.evaluate(Proposal("S", "x", 10), pf)["reason"] in ("within every limit", "DAILY_LOSS_LIMIT")
    assert not state.drawdown_halt()["engaged"]
    assert ledger.portfolio().peak_equity == 880.0 and state.take_peak_reset() is False   # used once
    state.engage_kill_switch("x")
    state.resume("RESUME")                                                      # a kill-switch release moves nothing
    assert state.take_peak_reset() is False


def test_open_checked_is_the_single_way_in(tmp_path):
    g, ledger, _ = _rig(tmp_path)
    d, pos = ledger.open_checked(g, Proposal("S", "m", 500), 0.5, instrument="tok", label="q")
    assert d["verdict"] == "REDUCED" and pos["stake"] == 20.0 and pos["decision_id"] == d["decision_id"]
    d, pos = ledger.open_checked(g, Proposal("NOPE", "m", 10), 0.5)
    assert d["verdict"] == "REJECTED" and pos is None
    d, pos = ledger.open_checked(g, Proposal("S", "m", 500), 0.5)
    assert pos["stake"] == 20.0                                                 # market cap: 40 in total
    assert ledger.open_checked(g, Proposal("S", "m", 500), 0.5)[1] is None


def test_cli_drill_never_touches_the_real_store(tmp_path, monkeypatch, capsys):
    db = tmp_path / "real.db"
    RiskState(db).engage_kill_switch("mine")
    before = db.read_bytes()
    monkeypatch.setenv("FRIDAY_RISK_DB", str(db))
    assert cli(["drill"]) == 0
    assert "RISK DRILLS: PASS" in capsys.readouterr().out and db.read_bytes() == before
    monkeypatch.setenv("FRIDAY_RISK_MAX_DRAWDOWN", "0.5")
    assert cli(["drill"]) == 2
