"""
Phase 8B — fair probability and edge against the live ask.

Principles under test:
- Fair value = live mid + the price band's measured bias, SHRUNK by the
  evidence (n / (n + 200)) and capped; no calibration data -> no adjustment
  and an explicit INSUFFICIENT_CALIBRATION status.
- Edge is measured against the ASK (what FRIDAY would pay) plus any fee,
  never against the mid; EDGE needs both a minimum size and >= 2 standard
  errors.
- Kelly is arithmetic only, fractional and capped; nothing can trade.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from friday.market.store import MarketStore
from friday.quant import QuantEngine
from friday.quant.fair import PRIOR_SAMPLES, EdgeSettings, FairValueModel, bin_table, kelly
from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
from friday.tests.test_quant_core import NOW, _book, _record

REF = datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat()
IN_A_WEEK = (NOW + timedelta(hours=168)).isoformat().replace("+00:00", "Z")     # 9B: inside the calibrated range


def calib(store, price, n, wins, horizon=168, prefix="c", question=None, neg_risk=None):
    """n resolved samples at `price` (YES tokens), `wins` of them won."""
    ids = [f"{prefix}{price}-{horizon}-{i}" for i in range(n)]
    with store._db() as db:                                         # batch insert (fast)
        db.executemany("INSERT INTO calib_markets(market_id, reference_time, volume, fetched_at, question, neg_risk)"
                       " VALUES(?,?,?,?,?,?)", [(m, REF, 5000, REF, question, neg_risk) for m in ids])
        for table in ("calib_points", "calib_points_end"):         # 8A anchor and 9B end anchor
            db.executemany(f"INSERT INTO {table}(market_id, token_id, payoff, horizon_hours, price, price_time)"
                           " VALUES(?,?,?,?,?,?)",
                           [(m, f"t{m}", 1.0 if i < wins else 0.0, horizon, price, REF) for i, m in enumerate(ids)])


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


def longshot_market(store, yes_mid=0.07, question=None, neg_risk=False, i=1):
    """A deep, eligible binary market with YES at 6c/8c and NO at 92c/94c."""
    m = {**gamma_market(i, tokens=("yes1", "no1")), "negRisk": neg_risk, "endDate": IN_A_WEEK,
         **({"question": question} if question else {})}
    from friday.market import parse_book, parse_market, snapshot_rows
    pm = parse_market(m)
    ts = int(NOW.timestamp() * 1000)
    books = {"yes1": parse_book(raw_book("yes1", [(round(yes_mid - 0.01, 3), 50000)], [(round(yes_mid + 0.01, 3), 50000)], ts=ts)),
             "no1": parse_book(raw_book("no1", [(round(1 - yes_mid - 0.01, 3), 50000)], [(round(1 - yes_mid + 0.01, 3), 50000)], ts=ts))}
    store.record([pm], snapshot_rows(pm, books, now=NOW), {"observed_at": NOW.isoformat(timespec="seconds")})
    return next(f for f in QuantEngine(store).features(now=NOW) if f.market_id == str(i))


# ================================================================ pieces

def test_bin_table_and_kelly():
    t = bin_table([{"price": 0.07, "payoff": 0.0}] * 98 + [{"price": 0.07, "payoff": 1.0}] * 2)
    b = t[2]                                                        # 0.05-0.10
    assert b["n"] == 100 and b["win_rate"] == 0.02 and round(b["bias"], 4) == -0.05 and b["se"] > 0
    assert bin_table([{"price": 0.5, "payoff": 1.0}] * 10)[7]["se"] > 0      # all-win bin: se never 0
    assert kelly(0.6, 0.5) == pytest.approx(0.2) and kelly(0.4, 0.5) == 0.0 and kelly(0.9, 1.0) == 0.0


def test_horizon_choice():
    h = FairValueModel.horizon_for
    assert h(20) == 24 and h(60) == 72 and h(150) == 168 and h(300) == 336 and h(800) == 720
    assert h(1080) == 720 and h(6) == 24                                # the edges of the calibrated range
    assert h(1081) is None and h(5) is None and h(None) is None and h(-3) is None    # 9B: no table is borrowed
    assert h(5000, "sports") == 24


def test_outside_the_calibrated_range_there_is_no_fair_value_claim(store):
    """9B: 8B used the 168h table for a market three months from its end."""
    calib(store, 0.07, 2000, 40)
    m = longshot_market(store)
    assert FairValueModel(store).evaluate(m)["status"] == "EDGE"
    m.hours_to_end = 2200.0
    e = FairValueModel(store).evaluate(m)
    assert e["status"] == "OUT_OF_CALIBRATED_RANGE" and e["fair"] == e["price"] and e["horizon_hours"] is None
    assert e["stake_fraction"] == 0.0 and e["z"] is None


def test_tables_use_the_end_anchored_points_only(store):
    with store._db() as db:                                             # resolution-anchored only (8A)
        db.executemany("INSERT INTO calib_markets(market_id, reference_time, volume, fetched_at) VALUES(?,?,?,?)",
                       [(f"r{i}", REF, 5000, REF) for i in range(500)])
        db.executemany("INSERT INTO calib_points(market_id, token_id, payoff, horizon_hours, price, price_time)"
                       " VALUES(?,?,?,?,?,?)", [(f"r{i}", f"tr{i}", 0.0, 168, 0.07, REF) for i in range(500)])
    model = FairValueModel(store)
    assert model.fair(0.07, 168)["bin_samples"] == 0                     # the leaky points are not used
    assert model.status()["anchor"] == "scheduled end date" and model.status()["reanchor_pending"] == 500


def test_fair_is_shrunk_by_evidence(store):
    calib(store, 0.07, 200, 4)                                      # bias -0.05 on 200 samples
    model = FairValueModel(store)
    f = model.fair(0.07, 168)
    assert f["weight"] == 0.5 and f["adjustment"] == -0.025 and f["fair"] == 0.045
    assert f["bin"] == "0.05-0.10" and f["bin_samples"] == 200 and f["horizon_hours"] == 168
    empty = model.fair(0.45, 168)
    assert empty["weight"] == 0 and empty["fair"] == 0.45 and empty["bin_samples"] == 0


def test_adjustment_is_capped(store):
    calib(store, 0.85, 5000, 0)                                     # absurd: nothing priced 85c ever won
    f = FairValueModel(store).fair(0.85, 168)
    assert f["adjustment"] == -0.15 and f["fair"] == 0.70


# ================================================================ edge

def test_edge_against_the_ask_with_significance(store):
    calib(store, 0.07, 2000, 40)                                    # long shots win 2%, priced 7%
    m = longshot_market(store)
    e = FairValueModel(store).evaluate(m)
    assert e["status"] == "EDGE" and e["best_side"] == "No"
    no = {s["outcome"]: s for s in e["sides"]}["No"]
    assert no["ask"] == 0.94 and no["fair"] == round(1 - e["fair"], 4)
    assert no["edge"] == round(no["fair"] - 0.94, 4) and no["z"] >= 2
    yes = {s["outcome"]: s for s in e["sides"]}["Yes"]
    assert yes["edge"] < 0 and yes["stake_fraction"] == 0.0
    assert no["kelly_full"] == round((no["fair"] - 0.94) / 0.06, 4)
    assert e["stake_fraction"] == 0.02                              # 1/4 Kelly, capped at 2%
    assert "UNVALIDATED" in e["label"] and "no execution authority" in e["label"]


def test_fees_thresholds_and_insufficient_data(store):
    calib(store, 0.07, 2000, 40)
    m = longshot_market(store)
    assert FairValueModel(store, EdgeSettings(fee=0.05)).evaluate(m)["status"] == "NO_EDGE"
    assert FairValueModel(store, EdgeSettings(min_edge=0.2)).evaluate(m)["status"] == "NO_EDGE"
    thin = MarketStore(store.path.parent / "thin.db")
    calib(thin, 0.07, 10, 0)
    assert FairValueModel(thin).evaluate(longshot_market(thin))["status"] == "INSUFFICIENT_CALIBRATION"
    none = MarketStore(store.path.parent / "none.db")
    e = FairValueModel(none).evaluate(longshot_market(none))
    assert e["status"] == "INSUFFICIENT_CALIBRATION" and e["fair"] == e["price"]
    assert e["edge"] == -0.01                                       # the spread is the only 'edge'


def test_only_eligible_binary_markets(store):
    m = longshot_market(store)
    m.eligible = False
    assert FairValueModel(store).evaluate(m) is None
    m.eligible = True
    m.outcomes = m.outcomes[:1]
    assert FairValueModel(store).evaluate(m) is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("FRIDAY_EDGE_FEE", "0.01")
    monkeypatch.setenv("FRIDAY_EDGE_MAX_STAKE", "0.01")
    s = EdgeSettings.from_environment()
    assert s.fee == 0.01 and s.max_stake == 0.01 and s.kelly_fraction == 0.25
    for bad in ({"fee": -1}, {"kelly_fraction": 0}, {"max_stake": 0.5}, {"min_edge": 0.9}):
        with pytest.raises(ValueError):
            EdgeSettings(**bad)


def test_model_cache_rebuilds(store):
    model = FairValueModel(store, cache_seconds=0)
    assert model.fair(0.07, 168)["bin_samples"] == 0
    calib(store, 0.07, 50, 1)
    assert model.fair(0.07, 168)["bin_samples"] == 50
    assert model.status()["samples_by_horizon"]["168h"] == 50


# ================================================================ context, CLI, app

def test_claude_context_carries_fair_value(store):
    from friday.market.context import MarketContext
    calib(store, 0.07, 2000, 40)
    longshot_market(store)
    block = MarketContext(store, engine=QuantEngine(store), fair_model=FairValueModel(store)).context_for("x", ["1"])
    fv = block["markets"][0]["quant"]["fair_value"]
    assert fv["status"] == "EDGE" and fv["best_side"] == "No" and "UNVALIDATED" in fv["label"]
    assert "ESTIMATED and UNVALIDATED" in block["notice"]


def test_cli_edges(store, capsys):
    from friday.quant.__main__ import main as cli
    calib(store, 0.07, 2000, 40)
    longshot_market(store)
    assert cli(["edges", "--db", str(store.path)]) == 0
    out = capsys.readouterr().out
    assert "EDGE" in out and "best No" in out and "no execution authority" in out


def test_app_scan_and_edges_endpoint(tmp_path, monkeypatch):
    from friday.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    calib(store, 0.07, 2000, 40)
    books = {"yes1": _fresh(raw_book("yes1", [(0.06, 50000)], [(0.08, 50000)])),
             "no1": _fresh(raw_book("no1", [(0.92, 50000)], [(0.94, 50000)]))}
    main = _app(tmp_path, monkeypatch, FakePolymarket([{**gamma_market(1), "endDate": IN_A_WEEK}], books), store)
    with TestClient(main.app) as c:
        [row] = c.get("/api/scan").json()
        assert row["edge_status"] == "EDGE" and row["edge_side"] == "No" and row["edge"] > 0.02
        edges = c.get("/api/quant/edges", params={"edges_only": True}).json()
        assert len(edges["edges"]) == 1 and "no execution authority" in edges["note"]
        assert edges["model"]["samples_by_horizon"]["168h"] == 2000
        assert c.get("/health").json()["execution_locked"] is True


# ================================================================ 8B.2: segments (the jarvis020 finding)

def test_segments_of_real_questions():
    from friday.quant.segments import segment_of
    for q in ("Chicago Cubs vs. San Diego Padres", "Spread: Rams (-2.5)", "Rams vs. Broncos: O/U 44.5",
              "Will Norway win on 2026-09-27?", "LoL: Cloud9 vs Team Liquid (BO5) - LCS Playoffs",
              "LoL: Cloud9 vs Team Liquid - Game 3 Winner"):
        assert segment_of(q) == "sports", q
    assert segment_of("Will Marine Le Pen win the 2027 French presidential election?", True) == "field"
    assert segment_of("Will Harry Kane win the 2026 Ballon d'Or?", 1) == "sports"        # 8C.2: a sports award
    assert segment_of("Who will be the next Prime Minister of Japan: Takaichi?", 1) == "field"
    assert segment_of("US announces end of Iranian blockade by December 31?", False) == "binary"
    assert segment_of(None) == "binary"


def test_no_segment_borrows_another_segments_bias(store):
    """8B.1 bug: one pooled 40-50c bias gave four MLB games an identical +6.17c edge."""
    calib(store, 0.45, 2000, 600, horizon=168, question="Will X happen by December 31?")   # binary: overpriced
    calib(store, 0.45, 2000, 600, horizon=24, question="Will X happen by December 31?")
    games = [longshot_market(store, yes_mid=0.45, i=i, question=f"Team {i} vs. Team {i + 10}") for i in (1, 2, 3, 4)]
    model = FairValueModel(store)
    for g in games:
        e = model.evaluate(g)
        assert g.segment == "sports" and e["segment"] == "sports" and e["horizon_hours"] == 24
        assert e["status"] == "INSUFFICIENT_CALIBRATION" and e["fair"] == e["price"]          # no borrowed bias
    event = longshot_market(store, yes_mid=0.45, i=7, question="Will Y happen by December 31?")
    assert model.evaluate(event)["status"] == "EDGE"                                          # its own segment


def test_sports_use_their_own_calibration_at_24h(store):
    calib(store, 0.07, 2000, 40, horizon=24, question="A vs. B")
    e = FairValueModel(store).evaluate(longshot_market(store, question="Team A vs. Team B"))
    assert e["segment"] == "sports" and e["horizon_hours"] == 24 and e["bin_samples"] == 2000
    assert e["status"] == "EDGE" and e["best_side"] == "No"


def test_calibration_by_segment(store):
    from friday.quant.calibration import build_calibration
    calib(store, 0.07, 40, 0, horizon=24, question="A vs. B")
    calib(store, 0.07, 60, 3, horizon=24, question="Will Z win?", neg_risk=1, prefix="f")
    cal = build_calibration(store, horizon_hours=24)
    assert cal["by_segment"]["sports"]["markets"] == 40 and cal["by_segment"]["field"]["markets"] == 60
    assert cal["by_segment"]["binary"]["markets"] == 0
    assert build_calibration(store, horizon_hours=24, segment="field")["samples"] == 60
    assert FairValueModel(store).status()["samples_by_segment"]["field/24h"] == 60
