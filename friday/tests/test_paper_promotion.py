"""Phase 12C: the owner's promotion of a forward test that succeeded, its automatic demotion, and the paper CLI."""

import pytest

from friday.lab import build_gate
from friday.market.store import MarketStore
from friday.paper import PROMOTE_WORD, BollingerResting, PaperEngine, sources
from friday.paper.__main__ import main as cli
from friday.risk import PaperLedger, RiskState

NAME = sources.NAME


def trade(pnl, per_share, day):
    return {"pnl": pnl, "per_share": per_share, "opened_at": f"2026-10-{day:02d}T10:00:00+00:00", "strategy": NAME}


GOOD = [trade(0.8, 0.02 + 0.001 * (i % 3), 1 + i % 20) for i in range(120)]
BAD = [trade(-0.3, -0.01 - 0.001 * (i % 3), 1 + i % 20) for i in range(120)]
FEW = [trade(1.0, 0.02, 1 + i % 5) for i in range(40)]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    RiskState(tmp_path / "r.db")
    engine = PaperEngine(None, PaperLedger(tmp_path / "r.db", 1000.0), None)
    record = {"trades": []}
    monkeypatch.setattr(engine, "trades", lambda limit=500: list(record["trades"]))
    return engine, BollingerResting(engine, None), record


def test_only_a_test_that_succeeded_can_be_promoted_and_only_with_the_word(rig):
    eng, src, record = rig
    with pytest.raises(PermissionError):
        src.promote("yes")
    for trades, status in ((FEW, "RUNNING"), (BAD, "FAILED"), ([trade(-10.0, -0.05, 1 + i) for i in range(6)], "STOPPED")):
        record["trades"] = trades
        with pytest.raises(ValueError, match=status):
            src.promote(PROMOTE_WORD)
        assert eng.promotion(NAME) is None
    record["trades"] = GOOD
    v = src.standing()
    assert v["status"] == "SUCCEEDED" and v["promoted"] is False and v["active"] is True
    g = src.gate_entry()
    assert g["status"] == "FORWARD_TEST" and "waiting for the owner" in g["reason"]       # success alone approves nothing
    v = src.promote(PROMOTE_WORD, "test")
    assert v["status"] == "APPROVED" and v["promoted"] and v["active"] and "promoted by the owner" in v["reason"]
    g = src.gate_entry()
    assert g["status"] == "APPROVED_FOR_PAPER" and g["evidence"]["promoted_by_owner"] is True
    assert '"trades": 120' in eng.promotion(NAME)["evidence"]                            # what it was promoted on is kept


def test_an_approved_strategy_no_longer_has_the_test_loss_limit_but_is_demoted_when_its_record_turns(rig):
    eng, src, record = rig
    record["trades"] = GOOD
    src.promote(PROMOTE_WORD)
    record["trades"] = GOOD + [trade(-12.0, 0.019, 21 + i % 3) for i in range(6)]         # -$72 since, still positive per share
    v = src.standing()
    assert v["pnl"] < -sources.LOSS_LIMIT + 96 and v["status"] == "APPROVED" and v["active"]
    record["trades"] = GOOD + [trade(-2.0, -0.2, 1 + i % 20) for i in range(200)]        # the whole record is now clearly negative
    v = src.standing()
    assert v["status"] == "DEMOTED" and not v["promoted"] and not v["active"]
    assert src.gate_entry()["status"] == "NOT_APPROVED" and eng.promotion(NAME)["revoked_at"]
    record["trades"] = GOOD                                                                # it does not come back by itself
    assert src.standing()["status"] == "DEMOTED" and src.on_run().get("inactive") is True
    with pytest.raises(ValueError, match="DEMOTED"):
        src.promote(PROMOTE_WORD)


def test_the_owner_can_revoke(rig):
    eng, src, record = rig
    record["trades"] = GOOD
    src.promote(PROMOTE_WORD)
    v = src.revoke("changed my mind")
    assert v["status"] == "DEMOTED" and "changed my mind" in v["reason"] and src.gate_entry()["status"] == "NOT_APPROVED"


def test_the_gate_approves_a_forward_test_only_on_the_owners_recorded_promotion(tmp_path, rig):
    eng, src, record = rig
    store = MarketStore(tmp_path / "m.db")
    fake = {"strategy": "SNEAKY", "status": "APPROVED_FOR_PAPER", "reason": "trust me", "evidence": {"forward": True}}
    lying = {**fake, "strategy": "LIAR", "evidence": {"forward": True, "promoted_by_owner": "yes"}}
    record["trades"] = GOOD
    waiting = src.gate_entry()
    g = build_gate(store, forward_tests=[fake, lying, waiting])
    assert g["approved_for_paper"] == [] and NAME in g["forward_test"] and "SNEAKY" not in str(g["strategies"])
    src.promote(PROMOTE_WORD)
    g = build_gate(store, forward_tests=[fake, src.gate_entry()])
    assert g["approved_for_paper"] == [NAME] and g["live_approved"] == [] and g["gate_version"] == "12C.1"
    assert NAME in g["summary"] and g["execution_locked"] is True


def test_cli_report_promote_and_revoke(tmp_path, monkeypatch, capsys):
    db = tmp_path / "risk.db"
    assert cli(["report", "--db", str(tmp_path / "none.db")]) == 2
    RiskState(db)
    PaperLedger(db, 1000.0)
    assert cli(["report", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "paper money only" in out and "no paper orders or trades yet" in out and f"forward test {NAME}: RUNNING" in out
    assert cli(["promote", "--db", str(db)]) == 3 and "REFUSED" in capsys.readouterr().out          # no word
    assert cli(["promote", "--db", str(db), "--confirm", "PROMOTE"]) == 3                           # not succeeded
    assert "only a forward test that has SUCCEEDED" in capsys.readouterr().out
    real = sources.verdict
    monkeypatch.setattr(sources, "verdict", lambda trades: real(GOOD))                              # as if 120 good trades were on record
    assert cli(["report", "--db", str(db)]) == 0
    assert "promote --confirm PROMOTE" in capsys.readouterr().out
    assert cli(["promote", "--db", str(db), "--confirm", "PROMOTE"]) == 0 and "PROMOTED" in capsys.readouterr().out
    assert cli(["report", "--db", str(db)]) == 0 and "APPROVED" in capsys.readouterr().out
    assert cli(["revoke", "--db", str(db), "--reason", "test"]) == 0 and "DEMOTED" in capsys.readouterr().out
    monkeypatch.undo()
    assert cli(["trades", "--db", str(db)]) == 0


def test_the_app_has_no_route_to_promote(monkeypatch):
    from fastapi.testclient import TestClient
    from friday.app import main
    with TestClient(main.app) as c:
        f = c.get("/api/paper").json()["forward_tests"][0]
        assert f["status"] == "RUNNING" and f["promoted"] is False
        for path in ("/api/paper/promote", "/api/paper", "/api/lab/gate"):
            assert c.post(path, json={"confirm": "PROMOTE"}).status_code in (404, 405)
    src = open(main.__file__).read()
    assert ".promote(" not in src and "record_promotion" not in src and "PROMOTE_WORD" not in src
