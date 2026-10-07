"""Phase 11C: the book-strategy test run - frozen holdout, development first, stored once, gate wiring."""

import json
import random
import threading

import pytest

from jarvis.lab import bookbt, bookrun
from jarvis.lab.bookbt import BookSettings, summarise, trades_for
from jarvis.lab.bookrun import (aggregate, fingerprint, freeze_boundary, gate_entries, merge, parts, render_test,
                                run_book_test, stored_results, summary)
from jarvis.market.store import MarketStore
from jarvis.strategies import LONG, STRATEGIES, Exit, Strategy

HOUR0 = 480_000


def fill(store, n=40, bars=400, seed=1, trend=0.0, start_day=0, start=0.5):
    """n markets of random-walk (or trending) hourly prices; market i resolves on day i."""
    for i in range(n):
        rng, p, series = random.Random(seed * 1000 + i), start, []
        for k in range(bars):
            p = min(0.94, max(0.06, p + rng.gauss(trend, 0.008)))
            series.append((HOUR0 + (start_day + i) * 24 + k, round(p, 3)))
        day = start_day + i
        store.record_bars({"market_id": f"s{seed}m{i}", "token_id": f"t{i}", "outcome": "Yes", "payoff": 1.0,
                           "question": f"q{i}", "reference_time": f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}T00:00:00+00:00",
                           "volume": 1e5, "neg_risk": 0}, series, bars, "OK")


def test_totals_match_the_trade_by_trade_summary():
    rng, p, c = random.Random(3), 0.5, []
    for _ in range(900):
        p = min(0.95, max(0.05, p + rng.gauss(0, 0.01)))
        c.append(round(p, 3))
    for st in STRATEGIES:
        if st.kind != "SIGNAL":
            continue
        halves = [trades_for(st, c[:450], list(range(450)), "a"), trades_for(st, c[450:], list(range(450, 900)), "b")]
        total = merge(aggregate(halves[0]), aggregate(halves[1]))
        for cost in (0.0, 0.01):
            a, b = summary(total, cost, 2.81, st.exit.stop), summarise(halves[0] + halves[1], cost, 2.81)
            for k in ("trades", "days", "avg_gross", "win_rate", "exits", "long", "short"):
                assert a[k] == b[k], (st.key, k)
            assert a["avg_net"] == pytest.approx(b["avg_net"], abs=2e-5) and a["avg_r"] == pytest.approx(b["avg_r"], abs=1e-3)
            if b["interval"]:
                assert a["interval"] == pytest.approx(b["interval"], abs=2e-5)
    assert summary(bookrun._empty(), 0.01, 2.81, 0.03)["trades"] == 0


def test_boundary_is_frozen_once_and_new_markets_cannot_move_it(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    with pytest.raises(ValueError):
        freeze_boundary(store)
    fill(store, 20)
    b = freeze_boundary(store)
    boundary, dev, hold = parts(store)
    assert boundary == b and len(dev) == 14 and len(hold) == 6
    fill(store, 30, seed=2, start_day=10)                      # 30 more arrive, most of them later
    b2, dev2, hold2 = parts(store)
    assert b2 == b and all(m["reference_time"] < b for m in dev2) and all(m["reference_time"] >= b for m in hold2)
    assert len(dev2) + len(hold2) == 50


def test_random_prices_fail_development_and_leave_the_holdout_untouched(tmp_path, monkeypatch):
    store = MarketStore(tmp_path / "m.db")
    fill(store, 60)
    seen = []
    real = bookrun.run_part
    monkeypatch.setattr(bookrun, "run_part", lambda s, markets, rules, *a, **k: (seen.append(len(markets)), real(s, markets, rules, *a, **k))[1])
    r = run_book_test(store, BookSettings(0.01), workers=1)
    assert seen == [42] and r["holdout_run_for"] == [] and r["passed"] == []          # development only
    assert r["development_markets"] == 42 and r["holdout_markets"] == 18 and len(r["strategies"]) == 11
    assert all(row["holdout"] is None and not row["verdict"].startswith("PASSED") for row in r["strategies"])
    assert all(row["development"]["avg_net"] < 0 for row in r["strategies"] if row["development"]["trades"] > 50)
    text = render_test(r)
    assert "HOLDOUT: not run" in text and "PASSED: none" in text and "Moving Averages" in text
    again = run_book_test(store, BookSettings(0.01), workers=1)                       # stored, not recomputed
    assert again["from_store"] is True and again["at"] == r["at"] and seen == [42]
    assert stored_results(store)["fingerprint"] == fingerprint() == r["fingerprint"]
    gate = gate_entries(store)
    assert len(gate) == 11 and all(g["status"] == "NOT_APPROVED" and g["evidence"]["book"] for g in gate)


def always_long(stop=0.03, target=0.02):
    return Strategy("BOOK_MOMENTUM", "Momentum", "SIGNAL", ("chan_qt",), "t", "t", "t", {}, Exit(stop, target, 6),
                    lambda c, p: LONG if len(c) % 12 == 0 else None)


def test_a_real_edge_passes_development_then_must_pass_the_holdout(tmp_path):
    up = MarketStore(tmp_path / "up.db")
    fill(up, 120, bars=250, trend=0.003, start=0.1)                                              # prices that really do drift up
    r = run_book_test(up, BookSettings(0.002), strategies=[always_long()], workers=1, save=False)
    [row] = r["strategies"]
    assert row["development"]["trades"] >= 200 and row["development"]["interval"][0] > 0
    assert r["holdout_run_for"] == ["BOOK_MOMENTUM"] and row["holdout"]["trades"] >= 60
    assert row["verdict"].startswith("PASSED (") and r["passed"] == ["BOOK_MOMENTUM"]
    assert "HOLDOUT (only rules that passed development)" in render_test(r)
    # the same rule where the drift stops at the holdout boundary: passes development, fails the holdout
    mixed = MarketStore(tmp_path / "mixed.db")
    fill(mixed, 84, bars=250, trend=0.003, start=0.1)
    fill(mixed, 36, bars=250, seed=9, trend=-0.0005, start_day=84)
    r2 = run_book_test(mixed, BookSettings(0.002), strategies=[always_long()], workers=1, save=False)
    assert r2["strategies"][0]["verdict"].startswith("FAILED (did not hold up") and r2["passed"] == []


def test_a_pass_reaches_the_gate_only_with_todays_settings(tmp_path, monkeypatch):
    store = MarketStore(tmp_path / "m.db")
    fill(store, 20)
    row = {"strategy": "BOOK_RSI", "name": "RSI", "verdict": "PASSED (development and holdout; paper trading must confirm)",
           "development": {"trades": 900, "days": 80, "markets": 300, "avg_net": 0.012, "interval": [0.004, 0.02],
                           "avg_gross": 0.032, "win_rate": 0.55},
           "holdout": {"trades": 300, "days": 30, "avg_net": 0.01, "interval": [0.002, 0.018]}}
    store.meta_set(bookrun.META_RESULTS, json.dumps({"fingerprint": fingerprint(), "cost_per_side": 0.01, "strategies": [row],
                                                     "passed": ["BOOK_RSI"], "at": "2026-10-06T00:00:00+00:00"}))
    [g] = gate_entries(store)
    assert g["status"] == "APPROVED_FOR_PAPER" and g["evidence"]["holdout"]["trades"] == 300
    from jarvis.lab import build_gate
    gate = build_gate(store)
    assert "BOOK_RSI" in gate["approved_for_paper"] and gate["live_approved"] == [] and gate["gate_version"] == "12C.1"
    assert "BOOK_RSI" in gate["summary"]
    from jarvis.lab.gate import render_gate
    assert "900 trades on 80 days" in render_gate(gate)
    monkeypatch.setattr(bookrun, "fingerprint", lambda *a: "changed")                 # someone edits a rule afterwards
    [g] = gate_entries(store)
    assert g["status"] == "NOT_APPROVED" and "re-tested" in g["reason"]
    store.meta_set(bookrun.META_RESULTS, "not json")
    assert stored_results(store) is None and gate_entries(store) == []


def test_two_builders_at_once_do_not_collide(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    m = {"market_id": "dup", "token_id": "t", "outcome": "Yes", "payoff": 1.0, "question": "q",
         "reference_time": "2026-01-01T00:00:00+00:00", "volume": 1.0, "neg_risk": 0}
    results = []

    def save(price):
        other = MarketStore(tmp_path / "m.db")                                        # a second process's own connection
        results.append(other.record_bars(m, [(HOUR0 + k, price) for k in range(200)], 200, "OK"))
    threads = [threading.Thread(target=save, args=(0.3 + 0.1 * i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 5 + [True]
    [s] = store.bar_series("OK")
    assert len(s["closes"]) == 200 and len(set(s["closes"])) == 1                     # one writer's series, whole
    assert store.bar_stats()["markets"] == {"OK": 1}


def test_workers_give_the_same_answer_as_one_process(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    fill(store, 90, bars=260)
    one = run_book_test(store, BookSettings(0.01), workers=1, save=False)
    two = run_book_test(store, BookSettings(0.01), workers=2, save=False)
    assert [r["development"] for r in one["strategies"]] == [r["development"] for r in two["strategies"]]


def test_cli_api_and_dashboard(tmp_path, capsys):
    from fastapi.testclient import TestClient
    from jarvis.app import main
    from jarvis.lab.__main__ import main as cli
    with TestClient(main.app) as c:
        assert c.get("/api/lab/book").json()["tested"] is False
        fill(main.quant.store, 30, bars=260)
        db = str(main.quant.store.path)
        assert cli(["book-results", "--db", db]) == 1
        assert cli(["book-test", "--db", db, "--workers", "1"]) == 0
        out = capsys.readouterr().out
        assert "BOOK STRATEGY TEST 11C.1" in out and "PASSED: none" in out and "development: 21 markets" in out
        assert cli(["book-results", "--db", db]) == 0 and "(stored result)" in capsys.readouterr().out
        assert cli(["bars-status", "--db", db]) == 0 and "boundary frozen" in capsys.readouterr().out
        b = c.get("/api/lab/book").json()
        assert b["tested"] and b["settings_current"] and len(b["strategies"]) == 11 and b["passed"] == []
        assert c.post("/api/lab/book", json={}).status_code == 405
        assert c.get("/api/lab/bars").json()["holdout_frozen"] is True
        main._backtest_cache.clear()
        d = c.get("/api/dashboard").json()
        assert d["book_strategies"]["tested"] == 11 and d["book_strategies"]["passed"] == []
        keys = [s["strategy"] for s in d["gate"]["strategies"]]
        assert "BOOK_RSI" in keys and "BOOK_RSI" in d["gate"]["not_approved"] and d["gate"]["approved_for_paper"] == []
        r = c.post("/api/risk/evaluate", json={"strategy": "BOOK_RSI", "market_id": "m", "stake": 10}).json()
        assert r["reason"] == "NOT_ALLOWED_BY_GATE"
