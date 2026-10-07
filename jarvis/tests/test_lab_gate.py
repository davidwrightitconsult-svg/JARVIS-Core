"""
Phase 9C — the validation gate and the pricing-inconsistency scan.

Principles under test:
- The gate gives every strategy exactly one status and can approve PAPER
  trading only; `live_approved` is always empty and execution stays locked.
- A strategy is approved only on evidence: enough scored signals over enough
  days with the whole 95% interval above zero (feature strategies), a robust
  out-of-sample backtest (calibration edge), or inconsistencies that are
  actually present in recorded order books.
- Research forecasts cannot be backtested without leaking outcomes: they
  are a FORWARD_TEST, allowed in paper trading as a hypothesis.
- With no evidence, nothing is approved — and the gate says so plainly.
- A pricing inconsistency is measured on recorded asks, counted in episodes
  (consecutive scans of one market), and ignores unusable quotes and
  out-of-scope markets.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jarvis.lab.gate import APPROVED, FORWARD, NOT_APPROVED, allowed, build_gate, render_gate
from jarvis.lab.inconsistency import render_inconsistencies, scan_inconsistencies
from jarvis.market.store import MarketStore

NOW = datetime.now(timezone.utc).replace(microsecond=0)
NOT_ROBUST = {"robust": False, "conclusion": "NOT SUPPORTED: no horizon passes out of sample on end-anchored prices.",
              "by_horizon": {"24h": {"markets": 6617, "bets": 1, "verdict": "INSUFFICIENT_DATA"}}}
ROBUST = {"robust": True, "conclusion": "ROBUST ON ASSUMED COSTS: passes out of sample at 24h, 72h.",
          "by_horizon": {"24h": {"markets": 6617, "bets": 400, "verdict": "PASSES_OUT_OF_SAMPLE"},
                         "72h": {"markets": 6000, "bets": 300, "verdict": "PASSES_OUT_OF_SAMPLE"}}}


@pytest.fixture
def store(tmp_path):
    return MarketStore(tmp_path / "m.db")


def book(store, run, market, asks, when, usable=1, question=None, depth=800):
    """One recorded run of a two-outcome market: asks = (yes_ask, no_ask); bids sit 2c below."""
    at = when.isoformat(timespec="seconds")
    with store._db() as db:
        if question:
            db.execute("INSERT OR IGNORE INTO markets(market_id, question, first_seen, last_seen) VALUES(?,?,?,?)",
                       (market, question, at, at))
        for token, ask in zip((f"{market}y", f"{market}n"), asks):
            db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid, spread,"
                       " depth_usd, flags, usable) VALUES(?,?,?,?,?,?,?,?,?,'[]',?)",
                       (run, at, token, market, round(ask - 0.02, 4), ask, round(ask - 0.01, 4), 0.02, depth, usable))


def signals(store, strategy, n, net, days=10, spread=0.004):
    """n scored signals over `days` days whose net result is `net` +/- `spread`."""
    sigs = []
    for i in range(n):
        at = (NOW - timedelta(days=1 + i * days / n)).isoformat(timespec="seconds")
        sigs.append({"run_id": i, "observed_at": at, "strategy": strategy, "strategy_version": "t", "market_id": f"m{i}",
                     "token_id": f"t{i}", "outcome": "Yes", "direction": "UP", "strength": 0.5, "horizon_hours": 1.0,
                     "expires_at": at, "mid": 0.5, "best_bid": 0.49, "best_ask": 0.51, "half_spread": 0.01, "inputs": {}})
    ids = store.record_signals(sigs)
    store.record_outcomes([{"signal_id": sid, "scored_at": NOW.isoformat(timespec="seconds"), "status": "SCORED",
                            "exit_mid": 0.5, "exit_bid": 0.5, "exit_ask": 0.5,
                            "mid_move": net, "net": round(net + (spread if k % 2 else -spread), 4), "cost": 0.51,
                            "return_on_cost": net / 0.51} for k, sid in enumerate(ids)])


# ================================================================ pricing inconsistencies

def test_both_outcomes_under_a_dollar_is_found_in_recorded_asks(store):
    t = NOW - timedelta(hours=5)
    book(store, 1, "a", (0.48, 0.49), t)                                     # 0.97: 3c locked
    book(store, 2, "a", (0.48, 0.50), t + timedelta(minutes=15))             # 0.98: still there on the next scan
    book(store, 3, "a", (0.50, 0.51), t + timedelta(minutes=30))             # gone
    book(store, 1, "b", (0.50, 0.51), t)                                     # normal: 1.01
    book(store, 4, "c", (0.30, 0.695), t + timedelta(minutes=45))            # 0.5c, once
    r = scan_inconsistencies(store)
    one = r["by_threshold"]["1c"]
    assert r["observations"] == 5 and r["runs"] == 4 and r["markets"] == 3 and r["ask_sum"]["min"] == 0.97
    assert (one["observations"], one["episodes"], one["markets"], one["max_locked"]) == (2, 1, 1, 0.03)
    assert one["median_runs"] == 2 and one["seen_once_only"] == 0 and one["examples"][0]["runs"] == 2
    assert r["by_threshold"]["0.5c"]["episodes"] == 2 and r["by_threshold"]["2c"]["observations"] == 2
    assert r["verdict"].startswith("RARE") and "VERDICT: RARE" in render_inconsistencies(r)


def test_fees_unusable_quotes_and_out_of_scope_markets(store):
    t = NOW - timedelta(hours=2)
    book(store, 1, "a", (0.48, 0.49), t)
    book(store, 1, "u", (0.10, 0.10), t, usable=0)                           # broken book: never an "opportunity"
    book(store, 1, "s", (0.40, 0.40), t, question="Tigers vs. Lions")        # sports: out of scope
    assert scan_inconsistencies(store)["observations"] == 1
    assert scan_inconsistencies(store, fee=0.015)["by_threshold"]["0.5c"]["observations"] == 0   # fees eat 3c
    assert scan_inconsistencies(store, excluded=frozenset())["observations"] == 2
    assert scan_inconsistencies(store, days=0.01)["verdict"].startswith("NO_DATA")


def test_present_needs_many_episodes_on_several_days(store):
    for d in range(6):
        for k in range(6):
            book(store, d * 10 + k, f"m{d}-{k}", (0.48, 0.49), NOW - timedelta(days=d + 1, minutes=k))
    r = scan_inconsistencies(store)
    assert r["by_threshold"]["1c"]["episodes"] == 36 and r["by_threshold"]["1c"]["days"] >= 5
    assert r["verdict"].startswith("PRESENT")
    clean = MarketStore(store.path.parent / "clean.db")
    book(clean, 1, "a", (0.50, 0.51), NOW - timedelta(hours=1))
    assert scan_inconsistencies(clean)["verdict"].startswith("NOT_OBSERVED")


# ================================================================ the gate

def test_with_no_evidence_nothing_is_approved_and_live_never_is(store):
    g = build_gate(store, robustness=NOT_ROBUST)
    by = {s["strategy"]: s for s in g["strategies"]}
    assert set(by) == {"CALIBRATION_EDGE", "BOOK_IMBALANCE", "MEAN_REVERSION", "MOMENTUM", "LONGSHOT_BIAS",
                       "PRICING_INCONSISTENCY", "RESEARCH_FORECAST"}
    assert g["approved_for_paper"] == [] and g["forward_test"] == ["RESEARCH_FORECAST"]
    assert g["live_approved"] == [] and g["execution_locked"] is True
    assert by["CALIBRATION_EDGE"]["status"] == NOT_APPROVED and by["CALIBRATION_EDGE"]["reason"].startswith("NOT SUPPORTED")
    assert "not enough evidence" in by["MOMENTUM"]["reason"] and by["RESEARCH_FORECAST"]["status"] == FORWARD
    assert g["summary"].startswith("No strategy has evidence of an edge")
    assert allowed(g, "RESEARCH_FORECAST") and not allowed(g, "CALIBRATION_EDGE") and not allowed(g, "ANYTHING_ELSE")
    text = render_gate(g)
    assert "live approved: none" in text and "execution is locked" in text


def test_a_feature_strategy_is_approved_only_on_enough_positive_evidence(store):
    signals(store, "MOMENTUM", 150, +0.02)                                   # +2c after the spread, 10 days
    signals(store, "MEAN_REVERSION", 150, -0.02)                             # loses after the spread
    signals(store, "BOOK_IMBALANCE", 150, 0.0, spread=0.03)                  # noise around zero
    signals(store, "LONGSHOT_BIAS", 40, +0.05)                               # looks great, far too few
    by = {s["strategy"]: s for s in build_gate(store, robustness=NOT_ROBUST)["strategies"]}
    assert by["MOMENTUM"]["status"] == APPROVED and by["MOMENTUM"]["evidence"]["days"] >= 7
    assert by["MEAN_REVERSION"]["status"] == NOT_APPROVED and "loses money" in by["MEAN_REVERSION"]["reason"]
    assert by["BOOK_IMBALANCE"]["status"] == NOT_APPROVED and "no measurable result" in by["BOOK_IMBALANCE"]["reason"]
    assert by["LONGSHOT_BIAS"]["status"] == NOT_APPROVED and "not enough evidence" in by["LONGSHOT_BIAS"]["reason"]
    g = build_gate(store, robustness=NOT_ROBUST)
    assert g["approved_for_paper"] == ["MOMENTUM"] and g["live_approved"] == [] and allowed(g, "MOMENTUM")
    assert g["summary"] == "Approved for PAPER trading: MOMENTUM."


def test_enough_signals_on_too_few_days_is_not_enough(store):
    signals(store, "MOMENTUM", 300, +0.02, days=2)
    s = {x["strategy"]: x for x in build_gate(store, robustness=NOT_ROBUST)["strategies"]}["MOMENTUM"]
    assert s["status"] == NOT_APPROVED and "not enough evidence" in s["reason"]


def test_calibration_edge_follows_the_robustness_report(store):
    assert build_gate(store, robustness=ROBUST)["approved_for_paper"] == ["CALIBRATION_EDGE"]
    real = build_gate(store)                                                 # computed from an empty store
    assert {s["strategy"]: s for s in real["strategies"]}["CALIBRATION_EDGE"]["status"] == NOT_APPROVED


def test_inconsistencies_are_approved_only_when_present(store):
    for d in range(6):
        for k in range(6):
            book(store, d * 10 + k, f"m{d}-{k}", (0.48, 0.49), NOW - timedelta(days=d + 1, minutes=k))
    g = build_gate(store, robustness=NOT_ROBUST)
    assert g["approved_for_paper"] == ["PRICING_INCONSISTENCY"] and g["live_approved"] == []


# ================================================================ CLI, app

def test_cli(store, capsys):
    from jarvis.lab.__main__ import main as cli
    book(store, 1, "a", (0.48, 0.49), NOW - timedelta(hours=1))
    assert cli(["gate", "--db", str(store.path)]) == 0
    out = capsys.readouterr().out
    assert "VALIDATION GATE 12C.1" in out and "FORWARD_TEST" in out and "RESEARCH_FORECAST" in out
    assert cli(["inconsistencies", "--db", str(store.path)]) == 0
    assert "PRICING INCONSISTENCIES 9C.1" in capsys.readouterr().out
    assert cli(["gate", "--db", str(store.path), "--json"]) == 0
    assert '"live_approved": []' in capsys.readouterr().out


def test_app_endpoints(tmp_path, monkeypatch):
    from jarvis.tests.test_market_data import FakePolymarket, _fresh, gamma_market, raw_book
    from jarvis.tests.test_quant_core import _app
    store = MarketStore(tmp_path / "d" / "m.db")
    books = {"yes1": _fresh(raw_book("yes1", [(0.46, 5000)], [(0.48, 5000)])),
             "no1": _fresh(raw_book("no1", [(0.47, 5000)], [(0.49, 5000)]))}       # asks sum to 0.97
    main = _app(tmp_path, monkeypatch, FakePolymarket([gamma_market(1)], books), store)
    with TestClient(main.app) as c:
        c.get("/api/scan")                                                   # records one run
        inc = c.get("/api/lab/inconsistencies").json()
        assert inc["observations"] == 1 and inc["by_threshold"]["2c"]["episodes"] == 1
        g = c.get("/api/lab/gate").json()
        assert g["gate_version"] == "12C.1" and g["live_approved"] == [] and g["forward_test"] == ["RESEARCH_FORECAST", "BOLLINGER_RESTING"]
        assert c.get("/health").json()["execution_locked"] is True
