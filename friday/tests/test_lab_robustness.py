"""
Phase 9B — end-anchored calibration data and the robustness report.

Principles under test:
- A price "h hours before the ACTUAL resolution" knows when the market
  resolved. The end-anchored point is the price h hours before the
  SCHEDULED end, and exists only if the market was still open then.
- Re-anchoring never invents a price, makes no request for a market that
  resolved long before any horizon, leaves a market pending on a transient
  error, and skips out-of-scope markets.
- The same backtest on both anchors shows the leak: an edge that exists
  only because the data knew the resolution time disappears on end-anchored
  prices.
- Where FRIDAY recorded the real order book at the decision time, the bet
  is priced at the recorded ask.
- "Robust" needs more than one horizon and more than one threshold.
- Nothing can trade.
"""

import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.lab.backtest import BacktestSettings, load_markets, run_backtest
from friday.lab.robustness import recorded_asks, render_robustness, run_robustness
from friday.market.backfill import END_HORIZONS, CalibrationBackfill
from friday.market.client import PolymarketData
from friday.market.store import MarketStore
from friday.tests.test_lab_backtest import T0, _rows, biased, insert

END = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


class History:
    """CLOB prices-history only (re-anchoring needs nothing else), with a call log."""

    def __init__(self, prices, fail=(), max_hours=200):
        self.prices, self.fail, self.max_hours, self.calls = prices, set(fail), max_hours, []

    def handler(self, request):
        p = dict(request.url.params)
        self.calls.append(p)
        if p["market"] in self.fail:
            return httpx.Response(500)
        a, b = int(p["startTs"]), int(p["endTs"])
        if b - a > self.max_hours * 3600:                                      # what jarvis020 showed: a long range is refused
            return httpx.Response(400, json={"error": "interval too long"})
        return httpx.Response(200, json={"history": [{"t": t, "p": v} for t, v in self.prices.get(p["market"], [])
                                                     if a <= t <= b]})

    def client(self):
        return PolymarketData(transport=httpx.MockTransport(self.handler))


def series(price, until, hours=800):
    ts = int(until.timestamp())
    return [(ts - h * 3600, price) for h in range(hours, -1, -1)]


def market(store, mid, end=END, closed=None, question="Will X happen by Friday?", yes_wins=True, neg_risk=0):
    """A calibration market as 8A stored it (resolution-anchored points only, end anchor pending)."""
    ref = min(end, closed) if closed else end
    with store._db() as db:
        db.execute("INSERT INTO calib_markets(market_id, question, neg_risk, end_date, closed_time, reference_time,"
                   " volume, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                   (mid, question, neg_risk, end.isoformat().replace("+00:00", "Z"),
                    closed.isoformat() if closed else None, ref.isoformat(timespec="seconds"), 5000, "x"))
        db.executemany("INSERT INTO calib_points(market_id, token_id, outcome, payoff, horizon_hours, price, price_time)"
                       " VALUES(?,?,?,?,?,?,?)",
                       [(mid, f"{mid}y", "Yes", 1.0 if yes_wins else 0.0, 24, 0.5, "x"),
                        (mid, f"{mid}n", "No", 0.0 if yes_wins else 1.0, 24, 0.5, "x")])
    return ref


def end_points(store, mid):
    with store._db() as db:
        return {(r["token_id"], r["horizon_hours"]): r["price"] for r in db.execute(
            "SELECT token_id, horizon_hours, price FROM calib_points_end WHERE market_id=?", (mid,))}


# ================================================================ re-anchoring

def test_on_schedule_market_gets_every_horizon_it_was_open_for(store):
    market(store, "a")
    h = History({"ay": series(0.8, END), "an": series(0.2, END)})
    summary = CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()
    assert summary["by_status"] == {"done": 1} and summary["points"] == 10 and summary["pending"] == 0
    assert {hh for _, hh in end_points(store, "a")} == set(END_HORIZONS)
    assert end_points(store, "a")[("ay", 720)] == 0.8
    assert len(h.calls) == 6                                    # per token: the last week + one small request per long horizon
    assert max(int(c["endTs"]) - int(c["startTs"]) for c in h.calls) <= 172 * 3600      # never a refused 30-day range
    assert CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()["stopped"] == "nothing pending"


def test_early_resolution_has_no_point_after_it_resolved(store):
    closed = END - timedelta(hours=48)                                        # resolved two days early
    market(store, "b", closed=closed)
    h = History({"by": series(0.85, closed), "bn": series(0.15, closed)})
    CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()
    got = {hh for _, hh in end_points(store, "b")}
    assert got == {72, 168, 336, 720}                                         # at end-24h it was already resolved
    with store._db() as db:
        t = db.execute("SELECT price_time FROM calib_points_end WHERE market_id='b' AND horizon_hours=72 LIMIT 1").fetchone()[0]
    assert datetime.fromisoformat(t) <= END - timedelta(hours=72)             # never a later price
    assert all(int(c["endTs"]) <= int(closed.timestamp()) for c in h.calls)   # nothing after the resolution is read


def test_resolved_months_early_costs_no_request(store):
    market(store, "c", end=END + timedelta(days=90), closed=END)              # "all-time high by December 31?" in October
    h = History({})
    summary = CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()
    assert summary["by_status"] == {"resolved before any horizon": 1} and h.calls == [] and end_points(store, "c") == {}


def test_out_of_scope_missing_end_and_no_price(store):
    market(store, "s", question="Tigers vs. Lions")
    market(store, "p")
    with store._db() as db:
        db.execute("INSERT INTO calib_markets(market_id, question, reference_time, fetched_at) VALUES('e','No end?','2026-09-01','x')")
        db.execute("INSERT INTO calib_points(market_id, token_id, payoff, horizon_hours, price, price_time)"
                   " VALUES('e','ey',1,24,0.5,'x')")
    h = History({})
    summary = CalibrationBackfill(h.client(), store, pace_seconds=0, exclude_segments={"sports"}).reanchor()
    assert summary["by_status"] == {"out of scope": 1, "no end date": 1, "no price at any horizon": 1}
    assert {c["market"] for c in h.calls} == {"py", "pn"}                     # only the in-scope, dated market is fetched


def test_transient_error_stays_pending_and_a_refused_range_is_final(store):
    market(store, "t")
    h = History({"ty": series(0.6, END), "tn": series(0.4, END)}, fail={"ty"})
    bf = CalibrationBackfill(h.client(), store, pace_seconds=0)
    first = bf.reanchor()
    assert first["errors"] and first["pending"] == 1 and first["stopped"] == "errors only"
    h.fail.clear()
    second = bf.reanchor()
    assert second["by_status"] == {"done": 1} and second["pending"] == 0
    assert {hh for _, hh in end_points(store, "t")} == set(END_HORIZONS)
    assert bf._history("ty", 0, 10 ** 9) == []                                # refused (400): nothing, not an error


def test_a_young_market_costs_no_long_horizon_requests(store):
    market(store, "y")                                                        # created 30 hours before its end
    h = History({"yy": series(0.5, END, hours=30), "yn": series(0.5, END, hours=30)})
    CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()
    assert {hh for _, hh in end_points(store, "y")} == {24} and len(h.calls) == 2


def test_9b1_results_are_redone_once(store):
    market(store, "a")
    store.record_end_anchor("a", [], "no price at any horizon")               # what the refused 30-day range left
    store.meta_set("end_anchor_rules", "9B.1")
    h = History({"ay": series(0.8, END), "an": series(0.2, END)})
    summary = CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()
    assert summary["redone"] == 1 and summary["by_status"] == {"done": 1}
    assert "redone" not in CalibrationBackfill(h.client(), store, pace_seconds=0).reanchor()


def test_background_batch_crawls_then_reanchors(store, monkeypatch):
    market(store, "a")
    h = History({"ay": series(0.8, END), "an": series(0.2, END)})
    bf = CalibrationBackfill(h.client(), store, pace_seconds=0)
    monkeypatch.setattr(bf, "run_windows", lambda *a, **kw: {"added": 0})
    summary = bf.run_all(max_markets=5)
    assert summary["reanchor"]["by_status"] == {"done": 1}
    assert bf.status()["end_anchor"]["pending"] == 0 and bf.status()["end_anchor"]["markets_by_horizon"]["24h"] == 1


# ================================================================ the leak

def leaky(store):
    """Deadline markets that resolved YES early: 24h before the ACTUAL resolution YES was 85c and always
    won. At 24h before the SCHEDULED end they were already resolved, so they have no end-anchored point.
    Plus fair on-schedule markets in both sets."""
    rng = random.Random(4)
    rows = [_rows(i, 0.85, True, i * 60 // 1500, prefix="e") for i in range(1500)]
    insert(store, rows, tables=("calib_points",))
    fair = []
    for i in range(1500):
        p = rng.choice([0.2, 0.5, 0.85])
        fair.append(_rows(i, p, rng.random() < p, i * 60 // 1500, prefix="f"))
    insert(store, fair)


def test_an_edge_that_only_exists_because_the_data_knew_the_resolution_time(store):
    leaky(store)
    res = run_backtest(store, BacktestSettings(anchor="resolution"))["out_of_sample"]
    end = run_backtest(store, BacktestSettings(anchor="end"))["out_of_sample"]
    assert res["bets"] > 300 and res["avg_net"] > 0.03 and res["verdict"].startswith("PASSES")    # what 9A saw
    assert not end["verdict"].startswith("PASSES") and (end["bets"] < 100)                         # what is real
    assert len(load_markets(store, 24, anchor="end")[0]) == 1500 < len(load_markets(store, 24, anchor="resolution")[0])


def test_robustness_report_shows_the_leak_and_refuses_to_call_it_robust(store):
    leaky(store)
    r = run_robustness(store)
    assert set(r["by_horizon"]) == {"24h", "72h", "168h", "336h", "720h"}
    leak = r["leak"]["24h"]
    assert leak["resolution_anchor"]["avg_net"] > 0.03 and (leak["net_difference"] is None or leak["net_difference"] > 0)
    assert r["robust"] is False and r["conclusion"].startswith("NOT SUPPORTED")
    assert set(r["thresholds"]) == {"horizon", "1c", "2c", "3c", "5c"}
    text = render_robustness(r)
    assert "THE LEAK" in text and "CONCLUSION: NOT SUPPORTED" in text and "Nothing here authorises any trade" in text


def test_robust_needs_several_horizons_and_thresholds(store):
    for k, h in enumerate((24, 72, 168)):
        biased(store, n=2400, days=80, win=0.02, seed=k, prefix=f"h{h}-", horizon=h)
    r = run_robustness(store)
    assert [h for h, v in r["by_horizon"].items() if v["verdict"].startswith("PASSES")] == ["24h", "72h", "168h"]
    assert r["robust"] is True and r["conclusion"].startswith("ROBUST ON ASSUMED COSTS")
    one = MarketStore(store.path.parent / "one.db")
    biased(one, n=2400, days=80, win=0.02)                                    # passes at one horizon only
    r1 = run_robustness(one)
    assert r1["robust"] is False and r1["conclusion"].startswith("FRAGILE")


# ================================================================ real asks

def record_quote(store, token, at, bid, ask):
    with store._db() as db:
        db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid, spread,"
                   " flags, usable) VALUES(1,?,?,?,?,?,?,?,'[]',1)",
                   (at.isoformat(timespec="seconds"), token, "m", bid, ask, (bid + ask) / 2, ask - bid))


def test_quote_near_is_the_last_usable_quote_before_the_moment(store):
    at = T0 + timedelta(hours=10)
    record_quote(store, "tok", at - timedelta(hours=5), 0.1, 0.2)             # too old
    assert store.quote_near("tok", at.isoformat(timespec="seconds")) is None
    record_quote(store, "tok", at - timedelta(hours=1), 0.90, 0.99)
    record_quote(store, "tok", at + timedelta(minutes=5), 0.5, 0.6)           # after the moment: never used
    assert store.quote_near("tok", at.isoformat(timespec="seconds"))["best_ask"] == 0.99


def test_bets_are_priced_at_friday_s_recorded_asks_where_it_has_them(store):
    biased(store, n=2400, days=80, win=0.02)                                  # NO looks 7c underpriced at 93c
    markets, _ = load_markets(store, 24, anchor="end")
    late = markets[-300:]
    for m in late:                                                            # but the real NO ask was 99c: a wide book
        for price, _, outcome, token in m["tokens"]:
            ask = 0.99 if outcome == "No" else 0.09
            record_quote(store, token, m["t"] - timedelta(minutes=20), ask - 0.12, ask)
    asks, gaps = recorded_asks(store, markets)
    assert len(asks) == 300 and max(gaps) == pytest.approx(0.06)
    real = run_robustness(store)["real_asks"]["24h"]
    assert real["recorded_markets"] == 300 and real["ask_minus_history_price"]["median"] > 0.01
    assert real["same_markets_at_assumed_asks"]["bets"] > 250                 # the assumed 1c made these look great
    assert real["at_recorded_asks"]["bets"] == 0                              # at the real ask there is nothing to buy
    assert run_robustness(MarketStore(store.path.parent / "none.db"))["real_asks"]["24h"] == {"recorded_markets": 0}


# ================================================================ CLI, app

def test_cli_robustness_and_anchor(store, capsys):
    from friday.lab.__main__ import main as cli
    leaky(store)
    assert cli(["robustness", "--db", str(store.path)]) == 0
    assert "ROBUSTNESS 9B.1" in capsys.readouterr().out
    assert cli(["backtest", "--db", str(store.path), "--anchor", "resolution"]) == 0
    assert "actual resolution (leaky)" in capsys.readouterr().out


def test_app_endpoints(tmp_path, monkeypatch):
    from friday.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
    from friday.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    leaky(store)
    books = {"yes1": _fresh(raw_book("yes1", [(0.48, 5000)], [(0.50, 5000)])),
             "no1": _fresh(raw_book("no1", [(0.50, 5000)], [(0.52, 5000)]))}
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], books), store)
    with TestClient(main.app) as c:
        r = c.get("/api/lab/robustness").json()
        assert r["robustness_version"] == "9B.1" and r["robust"] is False and "leak" in r
        assert c.get("/api/lab/backtest", params={"anchor": "resolution"}).json()["anchor"] == "resolution"
        assert c.get("/api/lab/backtest", params={"anchor": "x"}).status_code == 422
        assert c.post("/api/market/calibration/reanchor").status_code == 503      # backfill disabled in tests
        edges = c.get("/api/quant/edges").json()
        assert edges["model"]["anchor"] == "scheduled end date" and edges["model"]["reanchor_pending"] == 3000
        [row] = c.get("/api/scan").json()                                         # gamma_market ends in 2030
        assert row["edge_status"] == "OUT_OF_CALIBRATED_RANGE" and row["edge"] is None
        assert c.get("/health").json()["execution_locked"] is True
