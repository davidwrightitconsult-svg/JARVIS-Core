"""Phase 14B: the taker fee, and the size at which an edge stops existing.

14A stopped the engine pretending any size fills at the best price. It left trading free, which it
is not. Polymarket charges the taker `shares * rate * price * (1 - price)` -- quadratic in price,
worst at 0.50, vanishing in the tails -- at a rate that depends on the market's category.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.paper.capacity import replay, report
from jarvis.paper.costs import TAKER_RATES, UNKNOWN_RATE, CostSettings, cost_of, round_trip_per_share, taker_fee
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}
YES, MKT = "yes1", "m1"


# --------------------------------------------------------------------------- the fee formula
def test_the_fee_is_the_published_formula():
    # 100 shares at 0.50 in a 0.05 category: 100 * 0.05 * 0.50 * 0.50 = $1.25
    assert taker_fee(100, 0.50, 0.05) == pytest.approx(1.25)
    assert taker_fee(100, 0.50, 0.07) == pytest.approx(1.75)


def test_the_fee_peaks_at_the_middle_and_vanishes_in_the_tails():
    """This is why the fee matters so much to FRIDAY: the strategies that trade genuine uncertainty
    pay the most, and the ones that trade near-certainties pay almost nothing."""
    mid = taker_fee(100, 0.50, 0.05)
    for p in (0.10, 0.25, 0.75, 0.90):
        assert taker_fee(100, p, 0.05) < mid
    assert taker_fee(100, 0.98, 0.05) == pytest.approx(0.098)
    assert taker_fee(100, 0.02, 0.05) == pytest.approx(0.098)
    assert taker_fee(100, 0.50, 0.05) > 12 * taker_fee(100, 0.98, 0.05)


def test_a_fee_is_symmetric_about_a_half():
    assert taker_fee(100, 0.30, 0.05) == pytest.approx(taker_fee(100, 0.70, 0.05))


def test_nonsense_inputs_cost_nothing_rather_than_crashing():
    for args in ((0, 0.5, 0.05), (-1, 0.5, 0.05), (100, 0.0, 0.05), (100, 1.0, 0.05),
                 (100, 0.5, 0.0), (float("nan"), 0.5, 0.05), (100, float("inf"), 0.05)):
        assert taker_fee(*args) == 0.0


def test_an_unclassified_market_is_charged_the_worst_rate():
    """Guessing low on an unknown market would make it look like the cheapest thing to trade,
    which is exactly backwards."""
    s = CostSettings()
    assert s.rate_for("nonsense-category") == UNKNOWN_RATE == max(TAKER_RATES.values())
    assert s.rate_for(None) == UNKNOWN_RATE
    assert s.rate_for("crypto") == 0.07 and s.rate_for("politics") == 0.04


def test_the_multiplier_stresses_the_schedule_without_editing_it():
    assert CostSettings(fee_multiplier=1.5).rate_for("politics") == pytest.approx(0.06)
    assert CostSettings(enabled=False).rate_for("crypto") == 0.0


def test_gas_is_charged_per_fill_when_it_is_configured():
    free = cost_of(CostSettings(), 100, 0.50, "politics")
    assert free.gas_usd == 0.0, "the relayer sponsors gas for a proxy wallet"
    paid = cost_of(CostSettings(gas_usd_per_fill=0.25), 100, 0.50, "politics")
    assert paid.gas_usd == 0.25 and paid.total_usd == pytest.approx(free.total_usd + 0.25)
    assert paid.per_share == pytest.approx(paid.total_usd / 100)


def test_a_maker_fill_is_only_free_when_the_settings_say_so():
    """FRIDAY's resting strategies submit at market when they trigger, so they are takers. The flag
    exists for when a real resting limit order does, and defaults to charging the fee."""
    assert cost_of(CostSettings(), 100, 0.50, "crypto", maker=True).fee_usd > 0
    assert cost_of(CostSettings(credit_maker_fills=True), 100, 0.50, "crypto", maker=True).fee_usd == 0.0


def test_the_round_trip_cost_is_the_number_an_edge_must_beat():
    # Buy and sell at 0.50 in a 0.05 category: 2 * 0.05 * 0.25 = 2.5c a share, round trip.
    assert round_trip_per_share(CostSettings(), 0.50, "other") == pytest.approx(0.025)
    assert round_trip_per_share(CostSettings(), 0.95, "other") == pytest.approx(0.00475)


# --------------------------------------------------------------------------- the engine
class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def rig(tmp_path, question="Will it?", costs=None):
    clock = Clock()
    store = MarketStore(tmp_path / "market.db")
    engine = PaperEngine(RiskGovernor(RiskSettings(), RiskState(tmp_path / "risk.db"), lambda: GATE, clock),
                         PaperLedger(tmp_path / "risk.db", 1000.0, clock), store,
                         PaperSettings(costs=costs or CostSettings()), clock=clock)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)", (MKT, question, (clock() + timedelta(days=30)).isoformat(), at, at))
        db.execute("INSERT OR REPLACE INTO outcomes(token_id, market_id, name) VALUES(?,?,?)", (YES, MKT, "Yes"))
    return store, engine, clock


def book(store, clock, bids, asks):
    clock.advance(seconds=1)
    at = clock().isoformat(timespec="seconds")
    bid, ask = bids[0][0], asks[0][0]
    rows = [("bid", i, p, s) for i, (p, s) in enumerate(bids)] + \
           [("ask", i, p, s) for i, (p, s) in enumerate(asks)]
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                         " spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,?,?,?,?,'[]',1)",
                         (at, YES, MKT, bid, ask, (bid + ask) / 2, ask - bid))
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)", [(cur.lastrowid, sd, lv, p, s, at) for sd, lv, p, s in rows])


def plan(engine, position_id):
    import sqlite3
    with engine._db() as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM paper_plans WHERE position_id=?", (position_id,)).fetchone())


def test_the_booked_entry_price_carries_the_fee(tmp_path):
    store, engine, clock = rig(tmp_path)                     # "Will it?" -> category "other", 0.05
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED", r
    row = plan(engine, r["position"]["position_id"])
    assert row["fee_category"] == "other"
    assert row["entry_fill_price"] == 0.50, "the book itself charged 0.50"
    # 40 shares at 0.50 in a 0.05 category: 40 * 0.05 * 0.25 = $0.50, or 0.0125 a share.
    assert row["entry_fee_usd"] == pytest.approx(0.50, abs=0.02)
    assert r["position"]["entry_price"] > 0.50
    assert r["position"]["entry_price"] == pytest.approx(0.5125, abs=0.001)


def test_the_fee_reaches_the_ledgers_pnl_without_a_parallel_calculation(tmp_path):
    """Folding the fee into the booked price is the point: drawdown, the scorecard and the 9C gate
    all see net-of-fee numbers without any of them knowing fees exist."""
    store, engine, clock = rig(tmp_path)
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=1))
    clock.advance(hours=2)
    book(store, clock, [[0.50, 1000]], [[0.52, 1000]])       # the price did not move against us
    assert len(engine.tick()["closed"]) == 1
    closed = engine.ledger.positions("CLOSED")[0]
    assert closed["pnl"] < 0, "a flat round trip loses the fee, both ways"
    row = plan(engine, closed["position_id"])
    paid = float(row["entry_fee_usd"]) + float(row["exit_fee_usd"])
    assert paid > 0.9
    assert closed["pnl"] == pytest.approx(-paid, abs=0.02)


def test_the_category_sets_the_rate(tmp_path):
    cheap = rig(tmp_path / "a", question="Will Trump win the 2028 election?")     # politics, 0.04
    dear = rig(tmp_path / "b", question="Will Bitcoin close above $100k?")        # crypto, 0.07
    fees = []
    for store, engine, clock in (cheap, dear):
        book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
        r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
        row = plan(engine, r["position"]["position_id"])
        fees.append((row["fee_category"], row["entry_fee_usd"]))
    assert fees[0][0] == "politics" and fees[1][0] == "crypto"
    assert fees[1][1] == pytest.approx(fees[0][1] * 0.07 / 0.04, rel=1e-3)


def test_a_zero_fee_category_is_charged_nothing(tmp_path):
    store, engine, clock = rig(tmp_path, question="Will Russia and Ukraine sign a ceasefire?")
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    row = plan(engine, r["position"]["position_id"])
    assert row["fee_category"] == "geopolitics" and row["entry_fee_usd"] == 0.0
    assert r["position"]["entry_price"] == 0.50


def test_fees_can_be_turned_off_to_reproduce_a_pre_14b_record(tmp_path):
    store, engine, clock = rig(tmp_path, costs=CostSettings(enabled=False))
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["position"]["entry_price"] == 0.50
    assert plan(engine, r["position"]["position_id"])["entry_fee_usd"] == 0.0


def test_the_books_are_stored_so_the_trade_can_be_replayed_at_other_sizes(tmp_path):
    store, engine, clock = rig(tmp_path)
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=1))
    clock.advance(hours=2)
    book(store, clock, [[0.55, 1000]], [[0.57, 1000]])
    engine.tick()
    row = plan(engine, engine.ledger.positions("CLOSED")[0]["position_id"])
    assert '"asks"' in row["entry_book"] and '"bids"' in row["exit_book"]


# --------------------------------------------------------------------------- capacity
ENTRY = {"asks": [[0.50, 100], [0.52, 200], [0.55, 400]]}
EXIT = {"bids": [[0.56, 100], [0.54, 200], [0.50, 400]]}


def test_a_small_trade_clears_the_top_level_and_profits():
    sim = replay(ENTRY, EXIT, 20.0, CostSettings(), "politics")
    assert sim["entry_price"] == 0.50 and sim["exit_price"] == 0.56
    assert sim["net_usd"] > 0 and sim["complete"]


def test_the_same_edge_shrinks_as_the_size_grows():
    """Per dollar staked, a bigger order earns less: it pays more on the way in, receives less on
    the way out, and the fee is charged on all of it."""
    per_dollar = []
    for stake in (20.0, 50.0, 200.0):
        sim = replay(ENTRY, EXIT, stake, CostSettings(), "politics", max_slippage=0.10)
        per_dollar.append(sim["net_usd"] / sim["filled"])
    assert per_dollar == sorted(per_dollar, reverse=True), per_dollar


def test_a_size_the_book_cannot_absorb_is_not_a_breakeven_trade():
    """None means this trade would not have happened at this size. Scoring it as zero would make
    an unfillable size look harmless."""
    assert replay({"asks": [[0.50, 4]]}, EXIT, 500.0, CostSettings(), "politics") is None
    assert replay(ENTRY, None, 20.0, CostSettings(), "politics") is None
    assert replay("not json", EXIT, 20.0, CostSettings(), "politics") is None


def test_the_fee_alone_can_turn_a_real_edge_negative():
    """A 1c gross edge at 0.50 does not survive a 2.5c round trip. This is the whole reason the
    cost model had to exist before any strategy could be called profitable."""
    entry = {"asks": [[0.50, 10000]]}
    exit_ = {"bids": [[0.51, 10000]]}
    free = replay(entry, exit_, 100.0, CostSettings(enabled=False), "other")
    charged = replay(entry, exit_, 100.0, CostSettings(), "other")
    assert free["net_usd"] > 0
    assert charged["net_usd"] < 0


def test_the_report_names_the_largest_size_the_edge_survives():
    rows = [{"strategy": "S", "units": 40.0, "entry_fill_price": 0.50, "exit_fill_price": 0.56,
             "entry_fee_usd": 0.5, "exit_fee_usd": 0.5, "fee_category": "politics",
             "entry_book": ENTRY, "exit_book": EXIT} for _ in range(30)]
    out = report(rows, CostSettings(), ladder=(20.0, 100.0, 500.0, 5000.0), max_slippage=0.10)
    s = out["strategies"]["S"]
    assert s["trades"] == 30 and s["replayable"] == 30
    assert s["realised_gross_usd"] == pytest.approx(30 * 40 * 0.06)
    assert s["realised_fees_usd"] == pytest.approx(30.0)
    assert s["max_profitable_stake"] is not None
    rungs = {r["stake"]: r for r in s["rungs"]}
    # $5000 against a book holding ~$330 fills a fraction of the request. Reporting that rung as
    # profitable would claim capacity at a size that was never available.
    assert rungs[5000.0]["fill_ratio"] < 0.1
    assert rungs[5000.0]["profitable"] is False
    assert rungs[20.0]["fill_ratio"] == 1.0
    assert s["max_profitable_stake"] < 5000.0


def test_a_profitable_partial_fill_does_not_count_as_capacity():
    """The dangerous case: $500 requested, $100 available, and the $100 made money. Reporting that
    rung as profitable claims capacity at $500 that never existed."""
    rows = [{"strategy": "S", "units": 200.0, "entry_fill_price": 0.50, "exit_fill_price": 0.60,
             "entry_fee_usd": 0.0, "exit_fee_usd": 0.0, "fee_category": "geopolitics",
             "entry_book": {"asks": [[0.50, 200]]},
             "exit_book": {"bids": [[0.60, 10000]]}} for _ in range(20)]
    out = report(rows, CostSettings(), ladder=(100.0, 500.0))
    rungs = {r["stake"]: r for r in out["strategies"]["S"]["rungs"]}
    assert rungs[500.0]["net_usd"] > 0 and rungs[500.0]["ci95_low"] > 0
    assert rungs[500.0]["fill_ratio"] == pytest.approx(0.2)
    assert rungs[500.0]["profitable"] is False, "profit on a fifth of the order is not capacity"
    assert rungs[100.0]["profitable"] is True
    assert out["strategies"]["S"]["max_profitable_stake"] == 100.0


def test_a_rung_whose_interval_straddles_zero_is_not_called_profitable():
    """One lucky trade is not an edge. A positive total with an interval crossing zero means the
    sample cannot tell this apart from noise."""
    rows = [{"strategy": "S", "units": 1.0, "entry_fill_price": 0.5, "exit_fill_price": 0.5,
             "entry_fee_usd": 0.0, "exit_fee_usd": 0.0, "fee_category": "geopolitics",
             "entry_book": {"asks": [[0.50, 1000]]},
             "exit_book": {"bids": [[0.90 if i == 0 else 0.49, 1000]]}} for i in range(12)]
    out = report(rows, CostSettings(), ladder=(20.0,))
    rung = out["strategies"]["S"]["rungs"][0]
    assert rung["net_usd"] > 0 and rung["ci95_low"] < 0
    assert rung["profitable"] is False
    assert out["strategies"]["S"]["max_profitable_stake"] is None


def test_the_engine_report_runs_on_a_real_closed_trade(tmp_path):
    store, engine, clock = rig(tmp_path)
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=1))
    clock.advance(hours=2)
    book(store, clock, [[0.55, 1000]], [[0.57, 1000]])
    engine.tick()
    out = engine.capacity_report(ladder=(20.0, 100.0))
    s = out["strategies"]["S"]
    assert s["trades"] == 1 and s["replayable"] == 1
    assert s["realised_fees_usd"] > 0 and s["fee_share_of_gross"] is not None
    assert [r["stake"] for r in s["rungs"]] == [20.0, 100.0]
    assert out["costs"]["enabled"] is True
