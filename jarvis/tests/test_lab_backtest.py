"""
Phase 9A — walk-forward backtest of the calibration edge.

Principles under test:
- No look-ahead: a fold is tested with tables built only from markets that
  resolved before the fold's first price was observed.
- The backtest applies the LIVE rule (same arithmetic, thresholds, price
  band, scope) and prices bets at an assumed ask, held to resolution.
- A persistent bias passes out of sample; a bias that existed only in the
  past does not, even though it looks fine in sample.
- Bets resolving on the same day are not independent: the verdict uses the
  day-clustered interval and needs enough bets AND enough days.
- Sports stay out; results are split by market type.
- Nothing can trade.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jarvis.lab.backtest import BacktestSettings, _tables, _walk, decide, load_markets, render_text, run_backtest, summarise, verdict
from jarvis.market.store import MarketStore
from jarvis.quant.fair import EdgeSettings

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


def _rows(i, price, yes_wins, day, question=None, neg_risk=0, horizon=24, prefix="m"):
    mid, ref = f"{prefix}{i:05d}", (T0 + timedelta(days=day, minutes=i % 600)).isoformat(timespec="seconds")
    return ((mid, ref, 5000, ref, question or f"Will event {i} happen by Friday?", neg_risk, ref),
            [(mid, f"{mid}a", "Yes", 1.0 if yes_wins else 0.0, horizon, price, ref),
             (mid, f"{mid}b", "No", 0.0 if yes_wins else 1.0, horizon, round(1 - price, 4), ref)])


def insert(store, rows, tables=("calib_points", "calib_points_end")):
    with store._db() as db:
        db.executemany("INSERT INTO calib_markets(market_id, reference_time, volume, fetched_at, question, neg_risk,"
                       " end_date) VALUES(?,?,?,?,?,?,?)", [m for m, _ in rows])        # resolved on schedule
        for table in tables:
            db.executemany(f"INSERT INTO {table}(market_id, token_id, outcome, payoff, horizon_hours, price, price_time)"
                           " VALUES(?,?,?,?,?,?,?)", [p for _, pts in rows for p in pts])


def add(store, i, price, yes_wins, day, **kw):
    """One resolved two-outcome market: YES priced `price` at the horizon."""
    insert(store, [_rows(i, price, yes_wins, day, **kw)])


def biased(store, n=1200, days=60, win=0.02, price=0.07, seed=1, start=0, **kw):
    """YES long shots priced 7c that win only `win` of the time, spread over `days`."""
    rng = random.Random(seed)
    insert(store, [_rows(i, price, rng.random() < win, start + i * days // n, **kw) for i in range(n)])


# ================================================================ data

def test_load_keeps_only_in_scope_two_outcome_consistent_markets(store):
    add(store, 1, 0.3, True, 1)
    add(store, 2, 0.3, True, 2, question="Tigers vs. Lions")                       # sports: out of scope
    add(store, 3, 0.3, True, 3, question="Bitcoin Up or Down today?")
    with store._db() as db:
        db.execute("UPDATE calib_points SET price=0.9 WHERE token_id='m00001b'")   # 0.3 + 0.9: broken
        db.execute("INSERT INTO calib_markets(market_id, reference_time, fetched_at, question) VALUES('x','2026-06-01','x','One token only?')")
        db.execute("INSERT INTO calib_points(market_id, token_id, payoff, horizon_hours, price, price_time)"
                   " VALUES('x','xa',1,24,0.5,'x')")
    markets, dropped = load_markets(store, 24)
    assert [m["market_id"] for m in markets] == ["m00003"] and markets[0]["category"] == "crypto_micro"
    assert dropped == {"out of scope": 1, "not two outcomes": 1, "prices do not sum to 1": 1, "no resolution time": 0}
    assert len(load_markets(store, 24, frozenset())[0]) == 2                       # sports back when scope is cleared
    assert [m["market_id"] for m in load_markets(store, 24, anchor="end")[0]] == ["m00001", "m00003"]
    assert load_markets(store, 168)[0] == []                                       # another horizon: no data


# ================================================================ no look-ahead

def test_training_never_sees_the_test_fold_or_anything_later(store):
    biased(store, n=600, days=60)
    s = BacktestSettings(folds=5)
    markets, _ = load_markets(store, 24)
    _, folds = _walk(markets, s)
    assert [f["fold"] for f in folds] == [1, 2, 3, 4, 5]
    for f in folds:
        cutoff = datetime.fromisoformat(f["train_until"])                           # the fold's first decision
        assert cutoff == datetime.fromisoformat(f["test_from"]) - timedelta(hours=24)
        assert f["train_markets"] == sum(1 for m in markets if m["ref"] < cutoff)
        assert all(m["ref"] < cutoff for m in markets[:f["train_markets"]])
    assert folds[0]["train_markets"] < folds[-1]["train_markets"]                   # the window only grows


def test_a_fold_without_enough_history_is_skipped(store):
    biased(store, n=120, days=30)
    r = run_backtest(store, BacktestSettings(folds=5, min_train_markets=100))
    assert r["folds"][0]["skipped"].startswith("fewer than 100") and r["folds"][0]["bets"] == 0


# ================================================================ the rule

def test_decide_is_the_live_rule(store):
    biased(store, n=2000, days=40, win=0.02)
    markets, _ = load_markets(store, 24)
    tables = _tables(markets)
    s = BacktestSettings()
    m = {"market_id": "t", "question": "Will X happen?", "segment": "binary", "category": "other", "ref": T0,
         "tokens": [(0.07, 0.0, "Yes", "a"), (0.93, 1.0, "No", "b")]}
    bet = decide(m, tables, s)
    assert bet["outcome"] == "No" and bet["bought_at"] == 0.94 and bet["won"] and bet["net"] == 0.06
    assert bet["edge"] == round(bet["fair"] - 0.94, 4) and bet["z"] >= 2 and 0 < bet["stake"] <= 0.02
    lost = decide({**m, "tokens": [(0.07, 1.0, "Yes", "a"), (0.93, 0.0, "No", "b")]}, tables, s)
    assert lost["net"] == -0.94 and not lost["won"]
    assert decide(m, tables, s, half_spread=0.05) is None                           # costs eat the edge
    assert decide(m, tables, BacktestSettings(edge=EdgeSettings(min_edge=0.2))) is None
    assert decide({**m, "tokens": [(0.45, 0.0, "Yes", "a"), (0.55, 1.0, "No", "b")]}, tables, s) is None   # no data in that band
    assert decide({**m, "segment": "field"}, tables, s) is None                     # never another segment's bias


# ================================================================ outcomes

def test_a_persistent_bias_passes_out_of_sample(store):
    biased(store, n=3000, days=90, win=0.02)
    r = run_backtest(store)
    oos = r["out_of_sample"]
    assert oos["bets"] > 1500 and oos["win_rate"] > 0.95 and oos["avg_net"] > 0.02
    assert oos["ci95_net_clustered"][0] > 0 and oos["verdict"].startswith("PASSES_OUT_OF_SAMPLE")
    assert oos["bankroll_return"] > 0 and r["by_bought_price"][">=0.90"]["bets"] == oos["bets"]
    assert [x["half_spread"] for x in r["cost_sensitivity"]] == [0.0, 0.005, 0.01, 0.02, 0.03]
    nets = [x["avg_net"] for x in r["cost_sensitivity"] if x["bets"]]
    assert nets == sorted(nets, reverse=True)                                       # wider spread, less profit
    assert "OUT OF SAMPLE" in render_text(r) and "Nothing here authorises any trade" in render_text(r)


def test_a_bias_that_stopped_fails_out_of_sample_but_not_in_sample(store):
    biased(store, n=1500, days=40, win=0.02)                                        # the past: long shots overpriced
    biased(store, n=1500, days=40, win=0.20, start=40, seed=2, prefix="n")          # since then: UNDERpriced
    r = run_backtest(store, BacktestSettings(folds=2))
    oos = r["out_of_sample"]
    assert oos["bets"] > 500 and oos["avg_net"] < 0 and oos["verdict"] == "NEGATIVE_OUT_OF_SAMPLE"
    assert r["in_sample"]["bets"] < oos["bets"] or (r["in_sample"]["avg_net"] or 0) > oos["avg_net"]


def test_no_bias_means_no_bets_and_no_claim(store):
    rng = random.Random(3)
    rows = []
    for i in range(1500):
        p = rng.choice([0.1, 0.25, 0.4, 0.6, 0.75, 0.9])
        rows.append(_rows(i, p, rng.random() < p, i * 60 // 1500))
    insert(store, rows)
    r = run_backtest(store)
    assert r["out_of_sample"]["bets"] < 150                                         # noise may fire a few; no edge claimed
    assert not r["out_of_sample"]["verdict"].startswith("PASSES")


def test_only_the_live_price_band_is_traded(store):
    biased(store, n=2000, days=60, win=0.001, price=0.02)                           # the bias lives below 3c
    r = run_backtest(store)
    assert r["dataset"]["in_price_band"] == 0 and r["out_of_sample"]["bets"] == 0
    assert r["out_of_sample"]["verdict"].startswith("INSUFFICIENT_DATA")
    wide = run_backtest(store, BacktestSettings(min_price=0.0, max_price=1.0, half_spread=0.0,
                                                edge=EdgeSettings(min_edge=0.005, model_se=0.001)))
    assert wide["out_of_sample"]["bets"] > 0                                        # visible only outside the band


# ================================================================ statistics

def test_same_day_bets_are_one_observation(store):
    day = lambda d, net: {"ref": T0 + timedelta(days=d), "market_id": f"{d}-{net}", "net": net, "cost": 0.94,
                          "stake": 0.02, "won": net > 0}
    one_day = [day(0, 0.06)] * 39 + [day(0, -0.94)]
    st = summarise(one_day)
    assert st["bets"] == 40 and st["days"] == 1 and st["ci95_net_clustered"] is None
    assert st["verdict"].startswith("INSUFFICIENT_DATA")                            # 40 bets, but one day
    spread = [day(d % 10, 0.06) for d in range(60)]
    assert summarise(spread)["verdict"].startswith("PASSES") and summarise(spread)["days"] == 10
    # ten good days and one disastrous day: per-bet interval is tighter than the clustered one
    lumpy = [day(d % 10, 0.06) for d in range(100)] + [day(11, -0.94)] * 20
    st = summarise(lumpy)
    assert (st["ci95_net_clustered"][1] - st["ci95_net_clustered"][0]) > (st["ci95_net"][1] - st["ci95_net"][0])
    assert st["max_drawdown"] > 0 and st["verdict"] in ("INCONCLUSIVE", "NEGATIVE_OUT_OF_SAMPLE")
    assert verdict(100, 20, [-0.03, -0.01]) == "NEGATIVE_OUT_OF_SAMPLE" and verdict(10, 20, [0.01, 0.02]).startswith("INSUFF")
    assert summarise([])["bets"] == 0


def test_results_are_split_by_market_type(store):
    biased(store, n=1500, days=60, win=0.02, question="Will the Fed cut interest rates in March?")
    biased(store, n=1500, days=60, win=0.02, seed=5, prefix="s", question="Bitcoin Up or Down today?")
    r = run_backtest(store)
    assert set(r["by_category"]) == {"economy", "crypto_micro"}
    assert r["dataset"]["in_band_by_category"] == {"economy": 1500, "crypto_micro": 1500}
    assert all(v["bets"] > 300 for v in r["by_category"].values()) and set(r["by_segment"]) == {"binary"}


def test_settings_are_validated():
    for bad in ({"folds": 1}, {"half_spread": 0.5}, {"min_price": 0.9, "max_price": 0.1}, {"horizon_hours": 0}):
        with pytest.raises(ValueError):
            BacktestSettings(**bad)


# ================================================================ CLI, app

def test_cli(store, capsys):
    from jarvis.lab.__main__ import main as cli
    biased(store, n=900, days=45)
    assert cli(["backtest", "--db", str(store.path)]) == 0
    out = capsys.readouterr().out
    assert "WALK-FORWARD BACKTEST 9B.1" in out and "cost sensitivity" in out
    assert cli(["backtest", "--db", str(store.path), "--json"]) == 0
    assert '"out_of_sample"' in capsys.readouterr().out
    assert cli(["backtest", "--db", str(store.path.parent / "none.db")]) == 2


def test_app_endpoint(tmp_path, monkeypatch):
    from jarvis.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    biased(store, n=900, days=45)
    books = {"yes1": _fresh(raw_book("yes1", [(0.48, 5000)], [(0.50, 5000)])),
             "no1": _fresh(raw_book("no1", [(0.50, 5000)], [(0.52, 5000)]))}
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], books), store)
    with TestClient(main.app) as c:
        r = c.get("/api/lab/backtest").json()
        assert r["backtest_version"] == "9B.1" and r["out_of_sample"]["bets"] > 0 and len(r["folds"]) == 5
        assert c.get("/api/lab/backtest", params={"folds": 1}).status_code == 422
        assert c.get("/health").json()["execution_locked"] is True
