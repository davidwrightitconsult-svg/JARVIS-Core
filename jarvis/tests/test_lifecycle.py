"""Phase 17B: does the whole machine turn?

Every component is unit-tested in isolation. That is not the same claim as the chain working. A
mis-wired observer order, a settings object that never reaches the engine, a fill priced against
the wrong stake — each of those passes every unit test and breaks the platform.

So this drives the real store, governor, ledger, engine, executor and traders, and asserts what the
artifacts actually say at each stage: a signal becomes a depth-aware, fee-bearing position, which
exits into the real bids, which the scorer scores, which the reports describe. Then an arbitrage
pair, atomically, settling at exactly $1.

The market is synthetic. What is under test is the plumbing, not any strategy.
"""

import pytest

from jarvis.lab.lifecycle import ARB, ARB_MKT, ARB_NO, ARB_YES, STRATEGY, build, run, scan, signal
from jarvis.paper import Order
from jarvis.paper.executor import ExecutionSettings, Executor


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory):
    """One full run, shared. Nothing below mutates it."""
    return run(tmp_path_factory.mktemp("lifecycle"))


def stage(result, name):
    return next(t for t in result["trace"] if t["stage"] == name)


# --------------------------------------------------------------------------- the entry
def test_the_order_is_filled_by_walking_the_book_not_at_the_best_price(lifecycle):
    """$40 wanted where $5 rests at the best ask. The rest cannot be bought at that price."""
    seed, opened = stage(lifecycle, "seeded"), stage(lifecycle, "opened")
    assert opened["placed"] == 1
    assert opened["fill_price"] > seed["best_ask"], opened
    assert opened["levels_walked"] >= 2
    assert opened["slippage"] > 0


def test_the_stake_is_cut_below_what_was_requested(lifecycle):
    opened = stage(lifecycle, "opened")
    seed = stage(lifecycle, "seeded")
    assert opened["requested_stake"] == 40.0
    assert opened["stake"] < 40.0
    # Here the BOOK was the binding constraint, not the governor: only $15.30 rests inside the
    # slippage band, and the governor approved that in full.
    assert seed["capacity_in_band_usd"] == pytest.approx(15.30, abs=0.01)
    assert opened["stake"] == pytest.approx(seed["capacity_in_band_usd"], abs=0.01)


def test_the_fee_is_folded_into_the_booked_price(lifecycle):
    """The ledger's entry price carries the cost, so drawdown, the scorecard and the gate all see
    net-of-fee numbers without any of them knowing fees exist."""
    opened = stage(lifecycle, "opened")
    assert opened["fee_usd"] > 0 and opened["fee_category"] == "economy"
    assert opened["booked_price"] > opened["fill_price"]
    per_share = opened["fee_usd"] / (opened["stake"] / opened["fill_price"])
    assert opened["booked_price"] == pytest.approx(opened["fill_price"] + per_share, abs=1e-3)


def test_a_stop_price_is_a_price(lifecycle):
    """SignalTrader passes a stop DISTANCE of 0.999 to make the stop unreachable, which used to
    record a stop_price of -0.48: behaviourally fine, meaningless as data."""
    opened = stage(lifecycle, "opened")
    assert 0.0 <= opened["stop_price"] < 1.0


def test_the_executor_was_asked_before_anything_opened(lifecycle):
    assert stage(lifecycle, "executor_consulted")["decisions"]
    counts = stage(lifecycle, "reports")["executor_decisions"]
    assert f"{STRATEGY}:allowed" in counts


# --------------------------------------------------------------------------- the exit
def test_the_exit_walks_the_bids(lifecycle):
    exited = stage(lifecycle, "exited")
    assert exited["reason"] == "time"
    assert exited["exit_fill_price"] < exited["best_bid"], exited
    assert exited["exit_levels"] >= 2 and exited["exit_slippage"] > 0


def test_the_booked_pnl_is_the_gross_less_the_fees(lifecycle):
    """The arithmetic the whole platform rests on, checked once end to end."""
    exited = stage(lifecycle, "exited")
    assert exited["pnl"] == pytest.approx(exited["gross_pnl"] - exited["fees_total"], abs=0.01)


def test_the_fees_are_a_large_share_of_a_winning_trade(lifecycle):
    """Not a bug — the point of 14B. A trade that gained 1.2c a share gross keeps less than half."""
    exited = stage(lifecycle, "exited")
    assert exited["gross_pnl"] > 0 and exited["pnl"] > 0
    assert exited["fees_total"] / exited["gross_pnl"] > 0.4


def test_the_signal_is_scored_by_the_real_scorer(lifecycle):
    """The exit window is [expires_at, expires_at + max(1h, half the horizon)]. One second past it
    scores NO_EXIT_DATA, which is how the first run produced an empty edge report."""
    scored = stage(lifecycle, "scored")
    assert scored["by_status"] == {"SCORED": 1}
    assert lifecycle["edge"]["strategies"][STRATEGY]["scored"] == 1


# --------------------------------------------------------------------------- the arbitrage pair
def test_both_legs_open_on_one_governor_decision(lifecycle):
    arb = stage(lifecycle, "arbitrage_opened")
    assert arb["opened"] == 1 and arb["legs"] == 2
    assert arb["one_decision"] is True
    assert arb["equal_shares"] is True, "a pair is equal shares or it is a directional bet"


def test_the_pair_locks_the_gap_it_was_supposed_to(lifecycle):
    arb = stage(lifecycle, "arbitrage_opened")
    assert arb["ask_sum"] == pytest.approx(0.98)
    assert arb["fees_usd"] == 0.0, "geopolitics is a zero-fee category on the published schedule"
    assert arb["locked_usd"] > 0
    assert arb["locked_usd"] == pytest.approx(arb["pairs"] * (1 - arb["ask_sum"]), abs=0.05)


def test_the_pair_pays_exactly_one_dollar_a_pair_at_resolution(lifecycle):
    settled = stage(lifecycle, "arbitrage_settled")
    assert settled["closed"] == 1
    assert settled["payout_matches_pairs"] is True
    assert settled["realised"] == pytest.approx(settled["expected_locked"], abs=0.01)
    assert settled["slippage_vs_expected"] == pytest.approx(0.0, abs=0.01)


# --------------------------------------------------------------------------- the reports
def test_the_capacity_ladder_finds_the_size_the_edge_stops_surviving(lifecycle):
    """A real closed trade, re-walked at larger sizes against the books it really saw."""
    rungs = dict((stake, (net, ratio)) for stake, net, ratio in
                 stage(lifecycle, "reports")["capacity_rungs"])
    assert rungs[10.0][0] > 0 and rungs[10.0][1] == 1.0, "small and fully fillable: profitable"
    assert rungs[200.0][0] < 0, "large: the extra size is bought further down the book"
    assert rungs[200.0][1] < 0.2, "and most of it could not be filled at all"


def test_the_reports_describe_the_same_run(lifecycle):
    rep = stage(lifecycle, "reports")
    assert rep["mode"] == "PAPER" and rep["paper_version"] == "14B.1"
    assert rep["equity"] > 1000.0, "the run finished up: one winning trade and one locked pair"
    assert rep["edge_strategies"] == {STRATEGY: "INSUFFICIENT_DATA"}, "one signal is not evidence"
    # Arbitrage legs carry no plan row (that is how they stay out of reach of the engine's
    # exits), and the capacity report joins positions to plans — so they are absent by design.
    # Their own sizing question is answered by `price_pair` and the arbitrage report instead.
    assert rep["capacity_strategies"] == [STRATEGY]


def test_the_execution_report_answers_why_nothing_would_trade(lifecycle):
    ex = lifecycle["execution"]
    assert ex["enabled"] is True
    assert sorted(ex["gate_allows"]) == sorted([STRATEGY, ARB])
    assert set(ex["strategies"]) == {STRATEGY, ARB}


# --------------------------------------------------------------------------- the master switch
def test_with_execution_off_the_same_lifecycle_trades_nothing(tmp_path):
    """The one control that matters: everything wired, nothing traded."""
    rig = build(tmp_path)
    rig.executor.settings = ExecutionSettings(enabled=False)
    scan(rig, {"Yes": (0.48, 0.50, 4000.0), "No": (0.50, 0.52, 4000.0)})
    signal(rig)
    out = rig.signals.on_run()
    assert out["placed"] == 0 and out["refused"] == 1
    assert rig.ledger.positions("OPEN") == []

    scan(rig, {"Yes": (0.93, 0.95, 4000.0), "No": (0.01, 0.03, 4000.0)}, market_id=ARB_MKT,
         question="Will Russia and Ukraine sign a ceasefire?", end_hours=72)
    arb = rig.pairs.on_run()
    assert arb["opened"] == 0 and arb["refused"] == 1
    assert rig.ledger.positions("OPEN") == []
    assert rig.ledger.portfolio().equity == 1000.0, "and not a cent was committed"


def test_a_strategy_the_gate_does_not_allow_trades_nothing(tmp_path):
    rig = build(tmp_path)
    rig.executor = Executor(gate_provider=lambda: {"approved_for_paper": [], "forward_test": []},
                            settings=ExecutionSettings(enabled=True), clock=rig.clock)
    rig.signals.executor = rig.executor
    scan(rig, {"Yes": (0.48, 0.50, 4000.0), "No": (0.50, 0.52, 4000.0)})
    signal(rig)
    assert rig.signals.on_run()["placed"] == 0
    assert rig.ledger.positions("OPEN") == []


def test_a_thin_market_is_refused_rather_than_filled_generously(tmp_path):
    """14A's floor, reached through the real engine rather than a unit test."""
    rig = build(tmp_path)
    # Three levels of 2 shares each: $3.06 inside the slippage band, under the $5 floor.
    scan(rig, {"Yes": (0.48, 0.50, 2.0), "No": (0.50, 0.52, 2.0)})
    r = rig.engine.submit(Order(STRATEGY, "lc-m1", "lc-m1-yes", 40.0, stop=0.05,
                                target=None, max_hold_hours=24))
    assert r["status"] == "REFUSED" and "not enough depth" in r["reason"]
    assert rig.ledger.positions("OPEN") == []


def test_a_resolved_arbitrage_market_is_never_reopened(tmp_path):
    rig = build(tmp_path)
    scan(rig, {"Yes": (0.93, 0.95, 4000.0), "No": (0.01, 0.03, 4000.0)}, market_id=ARB_MKT,
         question="Will Russia and Ukraine sign a ceasefire?", end_hours=72)
    rig.store.record_resolution(ARB_MKT, {ARB_YES: 1.0, ARB_NO: 0.0})
    out = rig.pairs.on_run()
    assert out["considered"] == 0 and out["opened"] == 0
