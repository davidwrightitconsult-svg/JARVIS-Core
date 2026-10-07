"""Phase 11B: hourly bars, the book-strategy backtester, and the source check."""

import random
from datetime import datetime, timezone

import httpx
import pytest

from friday.lab import bookbt
from friday.lab.bookbt import BookSettings, render, run_book_backtest, split, summarise, trades_for, verdict
from friday.market.bars import CHUNK_HOURS, BarBuilder, regular
from friday.market.store import MarketStore
from friday.research.library import ResearchLibrary
from friday.strategies import LONG, SHORT, STRATEGIES, Exit, Strategy, books_in_library, check_sources

REF = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
REF_TS = int(REF.timestamp())


# ---------------------------------------------------------------- bars

def test_regular_buckets_fills_and_stops_at_the_end():
    h = 1000
    pts = [(h * 3600 + 10, 0.40), (h * 3600 + 3000, 0.42), ((h + 3) * 3600 + 5, 0.50), ((h + 9) * 3600, 0.9), (5, 1.7)]
    series, observed = regular(pts, h + 4)
    assert series == [(h, 0.42), (h + 1, 0.42), (h + 2, 0.42), (h + 3, 0.50)] and observed == 2   # last price in the hour;
    assert regular([], h) == ([], 0)                                             # gaps repeat; nothing after the end


class FakeHistory:
    """Hourly prices for `hours` before REF; refuses any range longer than a week, like the venue."""
    def __init__(self, hours=400, fail=()):
        self.hours, self.calls, self.fail = hours, [], set(fail)

    def price_history(self, token_id, start_ts, end_ts, fidelity_minutes=60):
        self.calls.append((token_id, start_ts, end_ts))
        request = httpx.Request("GET", "https://clob.example/prices-history")
        if token_id in self.fail:
            raise httpx.ConnectError("down", request=request)
        if end_ts - start_ts > CHUNK_HOURS * 3600:
            raise httpx.HTTPStatusError("too long", request=request, response=httpx.Response(400, request=request))
        first = REF_TS - self.hours * 3600
        return [(t, 0.5 + 0.1 * ((t // 3600) % 7) / 7) for t in range(max(start_ts, first) // 3600 * 3600 + 3600, end_ts + 1, 3600)
                if t >= first]


def seed(store, n=3, volume=50_000.0, question="Will event {} happen?"):
    for i in range(n):
        m = {"market_id": f"m{i}", "question": question.format(i), "reference_time": REF.isoformat(), "volume": volume + i,
             "neg_risk": 0, "end_date": REF.isoformat(), "closed_time": REF.isoformat()}
        store.record_calibration_market(m, [])
        store.record_end_anchor(f"m{i}", [
            {"token_id": f"no{i}", "outcome": "No", "payoff": 0.0, "horizon_hours": 24, "price": 0.4, "price_time": REF.isoformat()},
            {"token_id": f"yes{i}", "outcome": "Yes", "payoff": 1.0, "horizon_hours": 24, "price": 0.6, "price_time": REF.isoformat()}],
            "done")


def test_builder_reads_week_sized_requests_and_never_refetches(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed(store)
    client = FakeHistory(hours=400)
    b = BarBuilder(client, store, days=30, pace_seconds=0, exclude=frozenset())
    s = b.run(10)
    assert s["fetched"] == 3 and s["ok"] == 3 and s["stopped"] == "no markets left"
    assert all(t.startswith("yes") for t, _, _ in client.calls)                 # the Yes token, once per market
    assert all(e - a <= CHUNK_HOURS * 3600 for _, a, e in client.calls)
    assert len(client.calls) == 3 * 4                                           # 400h of history: 3 weeks, then an empty one
    [m, *_] = store.bar_series("OK")
    assert len(m["closes"]) == 400 and m["t"][-1] == REF_TS // 3600 and m["t"] == list(range(m["t"][0], m["t"][0] + 400))
    assert m["payoff"] == 1.0 and m["outcome"] == "Yes"
    before = len(client.calls)
    assert b.run(10)["fetched"] == 0 and len(client.calls) == before            # nothing is fetched twice
    stats = store.bar_stats()
    assert stats["markets"] == {"OK": 3} and stats["bars"]["OK"] == 1200 and stats["calibration_markets"] == 3


def test_builder_marks_thin_empty_out_of_scope_and_survives_errors(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed(store, 2)
    BarBuilder(FakeHistory(hours=60), store, pace_seconds=0, exclude=frozenset()).run(10)
    assert store.bar_stats()["markets"] == {"THIN": 2} and store.bar_series("OK") == []
    store2 = MarketStore(tmp_path / "m2.db")
    seed(store2, 2)
    BarBuilder(FakeHistory(hours=0), store2, pace_seconds=0, exclude=frozenset()).run(10)
    assert store2.bar_stats()["markets"] == {"EMPTY": 2}
    store3 = MarketStore(tmp_path / "m3.db")
    seed(store3, 2, question="Lakers vs. Celtics game {}")
    s = BarBuilder(FakeHistory(), store3, pace_seconds=0, exclude=frozenset({"sports"})).run(10)
    assert s["out_of_scope"] == 2 and s["fetched"] == 0 and store3.bar_stats()["markets"] == {"OUT_OF_SCOPE": 2}
    store4 = MarketStore(tmp_path / "m4.db")
    seed(store4, 2)
    client = FakeHistory(fail={"yes1"})
    b = BarBuilder(client, store4, pace_seconds=0, exclude=frozenset())
    import friday.market.bars as bars_mod
    orig, bars_mod.time.sleep = bars_mod.time.sleep, lambda s: None
    try:
        s = b.run(2)
    finally:
        bars_mod.time.sleep = orig
    assert s["ok"] == 1 and s["errors"] >= 1 and "ConnectError" in s["last_error"]
    assert [m["market_id"] for m in store4.bars_pending(10)] == ["m1"]          # left pending for a rerun
    assert BarBuilder(FakeHistory(), store4, pace_seconds=0, exclude=frozenset()).run(5, deadline_seconds=-1)["stopped"] == "deadline"


def test_split_is_chronological_and_fixed():
    series = [{"market_id": str(i), "reference_time": f"2026-01-{i + 1:02d}"} for i in range(20)]
    random.Random(1).shuffle(series)
    dev, hold = split(series)
    assert len(dev) == 14 and len(hold) == 6
    assert max(m["reference_time"] for m in dev) < min(m["reference_time"] for m in hold)
    assert split([]) == ([], [])


# ---------------------------------------------------------------- the trade simulator

def stub(fire, stop=0.03, target=0.06, hold=5, exit_at=None):
    """A rule that fires at chosen bars: {bar index: side}."""
    return Strategy("STUB", "Stub", "SIGNAL", ("murphy",), "test", "test", "test", {}, Exit(stop, target, hold, "x" if exit_at else ""),
                    lambda c, p: fire.get(len(c) - 1),
                    (lambda c, side, p: len(c) - 1 == exit_at) if exit_at else None)


def test_entry_is_the_next_bar_and_each_exit_is_the_unflattering_one():
    flat = [0.50] * 5
    [t] = trades_for(stub({2: LONG}), [0.50, 0.50, 0.50, 0.52, 0.53, 0.60, 0.70, 0.70])
    assert (t["entry_bar"], t["entry"]) == (3, 0.52)                            # decided at bar 2, filled at bar 3
    assert t["reason"] == "target" and t["gross"] == 0.06 and t["exit_bar"] == 5   # +8c seen, +6c booked
    [t] = trades_for(stub({2: LONG}), flat + [0.40, 0.50])
    assert t["reason"] == "stop" and t["gross"] == -0.10                        # gapped through the 3c stop: all 10c lost
    [t] = trades_for(stub({2: SHORT}), flat + [0.40, 0.50])
    assert t["reason"] == "target" and t["gross"] == 0.06 and t["side"] == SHORT
    [t] = trades_for(stub({2: SHORT}), flat + [0.56])
    assert t["reason"] == "stop" and t["gross"] == -0.06
    [t] = trades_for(stub({2: LONG}), [0.5, 0.5, 0.50, 0.50, 0.51, 0.51, 0.51, 0.51, 0.52, 0.58, 0.58])
    assert t["reason"] == "time" and t["bars_held"] == 5 and t["gross"] == 0.02
    [t] = trades_for(stub({2: LONG}, exit_at=5), [0.5, 0.5, 0.50, 0.50, 0.51, 0.52, 0.58])
    assert t["reason"] == "rule" and t["exit_bar"] == 5 and t["gross"] == 0.02
    [t] = trades_for(stub({2: LONG}), [0.5, 0.5, 0.50, 0.50, 0.51, 0.52])
    assert t["reason"] == "end of series" and t["gross"] == 0.02


def test_no_overlap_no_last_bar_entry_no_fill_outside_the_band():
    s = stub({2: LONG, 3: LONG, 4: LONG, 9: LONG}, hold=3)
    c = [0.5] * 12
    ts = trades_for(s, c)
    assert [(t["entry_bar"], t["exit_bar"]) for t in ts] == [(3, 6), (10, 11)]  # signals while in a position are ignored
    assert trades_for(stub({3: LONG}), [0.5] * 5) == []                         # would enter on the last bar: no trade
    assert trades_for(stub({2: LONG}), [0.5, 0.9, 0.94, 0.96, 0.96, 0.96]) == []     # the fill price left the band
    overlay = next(x for x in STRATEGIES if x.kind == "OVERLAY")
    assert trades_for(overlay, c) == []
    assert trades_for(s, c, hours=list(range(480, 492)), market_id="m")[0]["day"] == 20


def test_finished_trades_do_not_change_when_the_future_is_removed():
    rng, p, c = random.Random(4), 0.5, []
    for _ in range(600):
        p = min(0.97, max(0.03, p + rng.gauss(0, 0.01)))
        c.append(round(p, 3))
    traded = 0
    for st in STRATEGIES:
        if st.kind != "SIGNAL":
            continue
        full = trades_for(st, c)
        traded += bool(full)
        cut = trades_for(st, c[:400])
        done = [t for t in full if t["exit_bar"] < 399]
        assert [t for t in cut if t["exit_bar"] < 399] == done, st.key
    assert traded >= 9


def test_summary_costs_clusters_and_verdict():
    trades = [{"gross": 0.05, "day": d, "market_id": f"m{d % 7}", "entry_bar": 1, "reason": "target", "side": LONG, "stop": 0.03}
              for d in range(40) for _ in range(6)]
    s = summarise(trades, 0.01, bookbt.Z_DEVELOPMENT)
    assert s["trades"] == 240 and s["days"] == 40 and s["markets"] == 7 and s["avg_net"] == 0.03 and s["avg_gross"] == 0.05
    assert s["avg_r"] == 1.0 and s["win_rate"] == 1.0 and s["exits"] == {"target": 240} and s["long"] == 240
    assert verdict(s, None).startswith("PASSED_DEVELOPMENT")
    assert verdict(s, summarise(trades[:120], 0.01)).startswith("PASSED (")
    assert verdict(s, summarise(trades[:30], 0.01)).startswith("INSUFFICIENT_DATA")
    assert verdict(summarise(trades[:60], 0.01), None).startswith("INSUFFICIENT_DATA")
    lose = [{**t, "gross": -0.01} for t in trades]
    assert verdict(summarise(lose, 0.01, 2.81), None).startswith("FAILED (loses")
    assert summarise(trades, 0.03)["avg_net"] == -0.01                          # the edge is gone at 3c a side
    rng = random.Random(2)
    noisy = [{**t, "gross": rng.gauss(0.001, 0.05)} for t in trades]
    assert verdict(summarise(noisy, 0.0, 2.81), None).startswith("FAILED (no edge")
    assert verdict(s, summarise(noisy, 0.0)).startswith("FAILED (did not hold")
    # one lucky day must not look like evidence: the same profit all on a single day gives no interval
    assert summarise([{**t, "day": 1} for t in trades], 0.01)["interval"] is None
    empty = summarise([], 0.01)
    assert empty["trades"] == 0 and verdict(empty, None).startswith("INSUFFICIENT_DATA")
    with pytest.raises(ValueError):
        BookSettings(cost_per_side=0.5)


def test_random_walks_give_no_strategy_an_edge():
    """On prices with no pattern, no rule may show a profit: the simulator must not manufacture one."""
    series = []
    for k in range(40):
        rng, p, c = random.Random(100 + k), 0.2 + 0.6 * (k % 7) / 6, []
        for _ in range(500):
            p = min(0.99, max(0.01, p + rng.gauss(0, 0.012)))
            c.append(round(p, 3))
        series.append({"market_id": f"w{k}", "reference_time": f"2026-02-{k % 28 + 1:02d}", "closes": c,
                       "t": list(range(k * 37, k * 37 + 500))})
    r = run_book_backtest(series, BookSettings(0.01))
    assert r["markets"] == 40 and r["bars"] == 20000 and len(r["strategies"]) == 11 and r["execution_locked"] is True
    for row in r["strategies"]:
        assert row["trades"] > 30, row["name"]
        assert row["avg_gross"] < 0.006, row                                    # no free profit before costs
        assert row["avg_net"] < 0 and not verdict(row, None).startswith("PASSED"), row
        assert row["by_cost"]["0c"] == row["avg_gross"] and row["by_cost"]["2c"] < row["by_cost"]["1c"] < row["by_cost"]["0c"]
    text = render(r, "Random walks")
    assert "Random walks: 40 markets" in text and "Moving Averages" in text


# ---------------------------------------------------------------- sources

def test_books_match_on_file_name_and_tell_the_two_chan_books_apart():
    docs = [{"doc_id": "a", "kind": "book", "title": "5b0902f2d03a9ac8ea7677b63ccf8579", "author": None,
             "source": "Trade Your Way to Financial Freedom (Van K. Tharp).pdf", "original": "originals/x.pdf"},
            {"doc_id": "b", "kind": "book", "title": "Quantitative Trading: How to Build Your Own Algorithmic Trading Business",
             "author": "Ernest P. Chan", "source": "qt.pdf", "original": "o.pdf"},
            {"doc_id": "c", "kind": "book", "title": "Trading Price Action Reversals", "author": "", "source": "r.pdf", "original": "r.pdf"},
            {"doc_id": "d", "kind": "book", "title": "Trading Price Action TRENDS", "author": "", "source": "t.pdf", "original": "t.pdf"},
            {"doc_id": "e", "kind": "article", "title": "Wilder and Murphy compared", "author": "", "source": "u", "original": "u"}]
    rows = {r["key"]: r for r in books_in_library(docs)}
    assert rows["tharp"]["in_library"] and rows["chan_qt"]["in_library"] and not rows["chan"]["in_library"]
    assert rows["brooks"]["doc_ids"] == ["c", "d"] and not rows["murphy"]["in_library"] and not rows["wilder"]["in_library"]
    assert rows["chan"]["strategies"] == [] and "Quantitative Trading" in rows["chan"]["note"]
    assert rows["chan_qt"]["strategies"] == ["Bollinger Bands", "Momentum"]


def test_source_check_reports_locations_and_never_text(tmp_path):
    lib = ResearchLibrary(tmp_path / "library").init()
    murphy = tmp_path / "Technical Analysis of the Financial Markets (Murphy).md"
    murphy.write_text("# Chapter 4\n\n" + "Support and resistance levels reverse roles once broken. " * 30
                      + "\n\n# Chapter 10\n\n" + "The MACD line crossing its signal line is the basic signal. " * 30)
    assert lib.add(murphy, kind="book").status == "ADDED"
    report = check_sources(lib)
    rows = {r["key"]: r for r in report["strategies"]}
    assert rows["BOOK_SUPPORT_RESISTANCE"]["status"] == "FOUND_IN_SOURCE"
    found = rows["BOOK_SUPPORT_RESISTANCE"]["books"][0]["found"][0]
    assert found["passages"] >= 1 and found["where"]
    assert rows["BOOK_MACD"]["status"] == "FOUND_IN_SOURCE"
    assert rows["BOOK_REVERSAL"]["status"] == "NOT_FOUND_IN_SOURCE"              # the book is there; the idea is not
    assert rows["BOOK_PRICE_ACTION"]["status"] == "NOT_VERIFIED"                # Brooks is not in this library
    assert rows["BOOK_RSI"]["status"] == "NOT_FOUND_IN_SOURCE" and rows["BOOK_RSI"]["books"][0]["in_library"] is False
    assert "reverse roles" not in str(report) and "basic signal" not in str(report)
    assert all(r["status"] == "NOT_VERIFIED" for r in check_sources(None)["strategies"])
    assert lib.locate("nope", "support") == [] and lib.locate("x", "") == []


def test_api_and_cli(tmp_path, monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from friday.app import main
    from friday.lab.__main__ import main as lab_cli
    from friday.strategies.__main__ import main as cli
    with TestClient(main.app) as c:
        seed(main.quant.store, 10)
        BarBuilder(FakeHistory(), main.quant.store, pace_seconds=0, exclude=frozenset()).run(20)
        b = c.get("/api/lab/bars").json()
        assert b["markets"] == {"OK": 10} and b["development_markets"] == 7 and b["holdout_markets"] == 3
        assert c.post("/api/lab/bars", json={}).status_code == 405
        s = c.get("/api/strategies").json()
        assert len(s["sources"]) == 12 and len(s["books"]) == 8 and s["registry_version"] == "11B.1"
        d = c.get("/api/dashboard").json()["book_strategies"]
        assert d["items"][0]["source_status"] == "NOT_VERIFIED" and d["tested"] == 0
        db = str(main.quant.store.path)
    assert lab_cli(["bars-status", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "10 of 10 resolved markets fetched" in out and "7 development markets, 3 holdout markets" in out
    assert cli(["sources"]) == 0 and "NOT_VERIFIED" in capsys.readouterr().out
