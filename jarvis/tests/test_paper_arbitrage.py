"""Phase 15: the multi-leg arbitrage the 9C scan has been finding since Phase 9.

A two-outcome market pays exactly $1 to one side, so two asks summing to less than $1 lock in the
difference. The whole question is whether the gap survives depth (14A) and fees (14B) -- and near
0.50 it usually does not, because the fee peaks exactly where a balanced pair sits.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.paper.arbitrage import STRATEGY, PairTrader, price_pair
from jarvis.paper.costs import CostSettings
from jarvis.risk import LedgerError, PaperLedger, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": [STRATEGY], "forward_test": [], "live_approved": []}
YES, NO, MKT = "yes1", "no1", "m1"
FREE = CostSettings(enabled=False)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


# --------------------------------------------------------------------------- the pair arithmetic
def test_a_balanced_pair_locks_the_gap_when_there_are_no_fees():
    # 0.49 + 0.49 = 0.98: 2c a pair. $50 buys ~51 pairs.
    q = price_pair([[0.49, 1000]], [[0.49, 1000]], 50.0, FREE)
    assert q is not None and q.complete
    assert q.locked_per_pair == pytest.approx(0.02, abs=1e-4)
    assert q.cost_usd <= 50.0 + 1e-6
    assert q.payout_usd == pytest.approx(q.pairs)


def test_the_budget_is_never_exceeded():
    q = price_pair([[0.49, 1000]], [[0.49, 1000]], 50.0, FREE)
    assert q.cost_usd <= 50.0 + 1e-6
    assert q.cost_usd > 49.0, "and not much of it is left unused either"


def test_a_lopsided_pair_does_not_waste_the_budget_on_an_even_split():
    """0.95 against 0.03. Splitting the budget evenly between the legs would buy 26 pairs and leave
    most of it idle; sizing by the cost of a pair buys 51."""
    q = price_pair([[0.95, 10000]], [[0.03, 10000]], 50.0, FREE)
    assert q.pairs == pytest.approx(50.0 / 0.98, rel=1e-3)
    assert q.cost_usd == pytest.approx(50.0, abs=0.01)


def test_a_balanced_two_cent_gap_is_destroyed_by_fees():
    """The fee peaks at 0.50, and a balanced pair pays it on BOTH legs: 2 * 0.05 * 0.49 * 0.51 is
    2.5c against a 2c gap. This is the finding that makes the cost model load-bearing."""
    free = price_pair([[0.49, 10000]], [[0.49, 10000]], 100.0, FREE)
    charged = price_pair([[0.49, 10000]], [[0.49, 10000]], 100.0, CostSettings(), "other")
    assert free.locked_per_pair > 0
    assert charged.locked_per_pair < 0, charged.as_dict()


def test_the_same_gap_survives_fees_in_a_lopsided_market():
    """0.95 / 0.03 is the same 2c gross, but p(1-p) is small on both legs, so the fee is ~0.4c."""
    q = price_pair([[0.95, 10000]], [[0.03, 10000]], 100.0, CostSettings(), "other")
    assert q.locked_per_pair > 0.015, q.as_dict()


def test_a_zero_fee_category_keeps_the_whole_gap():
    q = price_pair([[0.49, 10000]], [[0.49, 10000]], 100.0, CostSettings(), "geopolitics")
    assert q.locked_per_pair == pytest.approx(0.02, abs=1e-4)


def test_an_incomplete_pair_is_not_a_pair():
    """One share of each is what pays $1. 100 of one leg and 10 of the other is a directional bet
    on 90 shares, so a size that cannot be matched on both legs is refused outright."""
    assert price_pair([[0.49, 10000]], [[0.49, 6]], 100.0, FREE, min_pairs=50.0) is None
    deep = price_pair([[0.49, 10000]], [[0.49, 6]], 100.0, FREE, min_pairs=5.0)
    assert deep is not None and deep.pairs == pytest.approx(6.0, abs=0.01) and deep.complete


def test_walking_the_book_eats_the_gap():
    """Only $4.90 rests at 0.49; the rest is at 0.52, where the pair costs 1.01 and loses money."""
    thin = price_pair([[0.49, 10], [0.52, 10000]], [[0.49, 10000]], 500.0, FREE,
                      max_slippage=0.10, min_pairs=5.0)
    assert thin is not None and thin.locked_per_pair < 0.02
    assert thin.leg_a_price > 0.49


def test_nonsense_inputs_price_nothing():
    for args in (([], [[0.49, 10]], 50.0), ([[0.49, 10]], [], 50.0),
                 ([[0.49, 10]], [[0.49, 10]], 0.0), ([[0.49, 10]], [[0.49, 10]], -5.0)):
        assert price_pair(*args, FREE) is None


# --------------------------------------------------------------------------- atomicity
@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    store = MarketStore(tmp_path / "market.db")
    ledger = PaperLedger(tmp_path / "risk.db", 1000.0, clock)
    governor = RiskGovernor(RiskSettings(), RiskState(tmp_path / "risk.db"), lambda: GATE, clock)
    engine = PaperEngine(governor, ledger, store, PaperSettings(), clock=clock)
    trader = PairTrader(engine, store, lambda: GATE, enabled=True, clock=clock)
    return store, engine, trader, clock


def seed(store, clock, a_book, b_book, question="Will it rain?", end_hours=240):
    """Two outcomes of one market, each with a recorded ask and its real depth."""
    clock.advance(seconds=1)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)",
                   (MKT, question, (clock() + timedelta(hours=end_hours)).isoformat(), at, at))
        db.executemany("INSERT OR REPLACE INTO outcomes(token_id, market_id, name) VALUES(?,?,?)",
                       [(YES, MKT, "Yes"), (NO, MKT, "No")])
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        for token, bk in ((YES, a_book), (NO, b_book)):
            ask = bk["asks"][0][0]
            bid = bk["bids"][0][0]
            cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask,"
                             " mid, spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,?,?,?,?,'[]',1)",
                             (at, token, MKT, bid, ask, (bid + ask) / 2, ask - bid))
            rows = [(cur.lastrowid, "bid", i, p, s, at) for i, (p, s) in enumerate(bk["bids"])] + \
                   [(cur.lastrowid, "ask", i, p, s, at) for i, (p, s) in enumerate(bk["asks"])]
            db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                           " VALUES(?,?,?,?,?,?)", rows)


def lopsided(size=10000):
    """0.95 / 0.03 -- a 2c gap that survives fees."""
    return ({"asks": [[0.95, size]], "bids": [[0.94, size]]},
            {"asks": [[0.03, size]], "bids": [[0.02, size]]})


def test_both_legs_open_or_neither(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    out = trader.on_run()
    assert out["opened"] == 1, out
    legs = engine.ledger.positions("OPEN")
    assert len(legs) == 2
    assert {l["instrument"] for l in legs} == {YES, NO}
    assert {l["strategy"] for l in legs} == {STRATEGY}
    # One decision, both legs: the governor approved the position, not two separate bets.
    assert len({l["decision_id"] for l in legs}) == 1
    assert legs[0]["units"] == pytest.approx(legs[1]["units"], rel=1e-3), "a pair is equal shares"


def test_a_failing_leg_leaves_no_position_behind(rig):
    """The guarantee the whole phase rests on: one leg filled and the other not is a naked bet."""
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    decision = {"verdict": "APPROVED", "approved_stake": 100.0, "mode": "PAPER", "decision_id": 999,
                "strategy": STRATEGY, "market_id": MKT, "venue": "polymarket"}
    good = {"stake": 50.0, "price": 0.95, "instrument": YES}
    bad = {"stake": 50.0, "price": 0.0, "instrument": NO}        # a price of zero is rejected
    with pytest.raises(LedgerError):
        engine.ledger.open_legs(decision, [good, bad])
    assert engine.ledger.positions("OPEN") == []
    assert engine.ledger.portfolio().equity == 1000.0, "and the cash was not spent"


def test_a_failure_part_way_through_rolls_both_legs_back(rig, monkeypatch):
    """The validation above refuses before the first insert. This is the other half: a failure
    AFTER the rows are written must still leave nothing, which is what the single transaction buys."""
    store, engine, trader, clock = rig
    decision = {"verdict": "APPROVED", "approved_stake": 100.0, "mode": "PAPER", "decision_id": 997,
                "strategy": STRATEGY, "market_id": MKT, "venue": "polymarket"}
    monkeypatch.setattr(type(engine.ledger), "_row",
                        staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))))
    with pytest.raises(RuntimeError):
        engine.ledger.open_legs(decision, [{"stake": 50.0, "price": 0.95, "instrument": YES},
                                           {"stake": 50.0, "price": 0.03, "instrument": NO}])
    monkeypatch.undo()
    assert engine.ledger.positions("OPEN") == []
    assert engine.ledger.portfolio().equity == 1000.0


def test_a_resolved_market_is_never_opened_even_though_it_still_shows_a_gap(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    store.record_resolution(MKT, {YES: 1.0, NO: 0.0})
    out = trader.on_run()
    assert out["considered"] == 0 and out["opened"] == 0
    assert engine.ledger.positions("OPEN") == []


def test_the_legs_may_not_exceed_what_the_governor_approved(rig):
    store, engine, trader, clock = rig
    decision = {"verdict": "APPROVED", "approved_stake": 50.0, "mode": "PAPER", "decision_id": 998,
                "strategy": STRATEGY, "market_id": MKT, "venue": "polymarket"}
    with pytest.raises(LedgerError, match="more than"):
        engine.ledger.open_legs(decision, [{"stake": 40.0, "price": 0.5, "instrument": YES},
                                           {"stake": 40.0, "price": 0.5, "instrument": NO}])
    assert engine.ledger.positions("OPEN") == []


def test_a_decision_cannot_open_two_sets_of_legs(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    assert trader.on_run()["opened"] == 1
    with engine._db() as db:
        did = db.execute("SELECT decision_id FROM paper_arb_pairs").fetchone()["decision_id"]
    decision = {"verdict": "APPROVED", "approved_stake": 50.0, "mode": "PAPER", "decision_id": did,
                "strategy": STRATEGY, "market_id": MKT, "venue": "polymarket"}
    with pytest.raises(LedgerError, match="already been used"):
        engine.ledger.open_legs(decision, [{"stake": 10.0, "price": 0.5, "instrument": YES},
                                           {"stake": 10.0, "price": 0.5, "instrument": NO}])


# --------------------------------------------------------------------------- the trader
def test_a_gap_that_does_not_survive_fees_is_refused_with_the_arithmetic(rig):
    store, engine, trader, clock = rig
    seed(store, clock, {"asks": [[0.49, 10000]], "bids": [[0.48, 10000]]},
         {"asks": [[0.49, 10000]], "bids": [[0.48, 10000]]})
    out = trader.on_run()
    assert out["opened"] == 0 and out["refused"] == 1
    assert "does not survive fees" in out["pairs"][0]["reason"]
    assert engine.ledger.positions("OPEN") == []
    with engine._db() as db:
        reason = db.execute("SELECT reason FROM paper_refusals ORDER BY id DESC LIMIT 1").fetchone()["reason"]
    assert "does not survive fees" in reason


def test_no_gap_is_not_a_candidate_at_all(rig):
    store, engine, trader, clock = rig
    seed(store, clock, {"asks": [[0.52, 10000]], "bids": [[0.50, 10000]]},
         {"asks": [[0.52, 10000]], "bids": [[0.50, 10000]]})       # sums to 1.04
    out = trader.on_run()
    assert out["considered"] == 0 and out["opened"] == 0


def test_a_market_with_no_recorded_depth_is_refused(rig):
    store, engine, trader, clock = rig
    clock.advance(seconds=1)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)", (MKT, "Will it?", (clock() + timedelta(days=9)).isoformat(), at, at))
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        for token, bid, ask in ((YES, 0.94, 0.95), (NO, 0.02, 0.03)):
            db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                       " spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,?,?,?,?,'[]',1)",
                       (at, token, MKT, bid, ask, (bid + ask) / 2, ask - bid))
    out = trader.on_run()
    assert out["refused"] == 1 and "depth" in out["pairs"][0]["reason"]


def test_one_pair_per_market_at_a_time(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    assert trader.on_run()["opened"] == 1
    seed(store, clock, *lopsided())                 # the gap is still there on the next scan
    second = trader.on_run()
    assert second["considered"] == 0 and second["opened"] == 0
    assert len(engine.ledger.positions("OPEN")) == 2


def test_the_pair_is_held_to_resolution_and_pays_exactly_one(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    trader.on_run()
    with engine._db() as db:
        row = dict(db.execute("SELECT * FROM paper_arb_pairs").fetchone())
    assert row["locked_usd"] > 0

    store.record_resolution(MKT, {YES: 1.0, NO: 0.0})
    clock.advance(days=11)
    out = trader.on_run()
    assert out["closed"] == 1
    assert engine.ledger.positions("OPEN") == []
    closed = engine.ledger.positions("CLOSED")
    assert len(closed) == 2
    payout = sum(c["proceeds"] for c in closed)
    assert payout == pytest.approx(row["pairs"], rel=1e-6), "a complete pair pays $1 whichever way"
    assert sum(c["pnl"] for c in closed) == pytest.approx(row["locked_usd"], abs=0.01)


def test_it_pays_the_same_if_the_other_outcome_wins(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    trader.on_run()
    with engine._db() as db:
        pairs = db.execute("SELECT pairs FROM paper_arb_pairs").fetchone()["pairs"]
    store.record_resolution(MKT, {YES: 0.0, NO: 1.0})              # the unlikely side
    trader.on_run()
    assert sum(c["proceeds"] for c in engine.ledger.positions("CLOSED")) == pytest.approx(pairs, rel=1e-6)


def test_the_engines_own_exits_never_touch_an_arbitrage_leg(rig):
    """A stop, a target, the time limit or 13B settlement would each hand the edge straight back.
    Arb legs carry no plan row, which is how they stay out of reach of all four."""
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided(), end_hours=1)                   # inside the settlement window
    trader.on_run()
    clock.advance(hours=1)
    summary = engine.tick()
    assert summary["closed"] == [] and summary["settled"] == 0
    assert len(engine.ledger.positions("OPEN")) == 2


def test_nothing_trades_without_the_gate(rig):
    store, engine, _t, clock = rig
    seed(store, clock, *lopsided())
    for provider in (None, lambda: {}, lambda: {"approved_for_paper": ["SOMETHING_ELSE"]},
                     lambda: (_ for _ in ()).throw(RuntimeError("gate is down")), lambda: "not a dict"):
        trader = PairTrader(engine, store, provider, enabled=True, clock=clock)
        out = trader.on_run()
        assert out["opened"] == 0 and "inactive" in out, provider
        assert trader.allowed() is False
    assert engine.ledger.positions("OPEN") == []


def test_it_is_off_unless_turned_on(rig):
    store, engine, _t, clock = rig
    seed(store, clock, *lopsided())
    trader = PairTrader(engine, store, lambda: GATE, clock=clock)   # enabled defaults to False
    assert trader.on_run()["inactive"] == "disabled"
    assert engine.ledger.positions("OPEN") == []


def test_the_report_separates_what_was_expected_from_what_was_realised(rig):
    store, engine, trader, clock = rig
    seed(store, clock, *lopsided())
    trader.on_run()
    mid = trader.report()
    assert mid["open"] == 1 and mid["closed"] == 0
    assert mid["expected_locked_usd"] > 0 and mid["realised_usd"] is None
    assert mid["fees_usd"] > 0 and mid["allowed_now"] is True

    store.record_resolution(MKT, {YES: 1.0, NO: 0.0})
    trader.on_run()
    end = trader.report()
    assert end["open"] == 0 and end["closed"] == 1
    assert end["realised_usd"] == pytest.approx(end["expected_locked_usd"], abs=0.01)
    assert end["slippage_vs_expected"] == pytest.approx(0.0, abs=0.01)
