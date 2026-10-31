"""Phase 14A: fills walk the recorded book instead of taking any size at the best price.

Two halves. The first tests jarvis/paper/fills.py as arithmetic, where the answers can be checked
by hand. The second drives the paper engine against books of known shape and asserts that the
recorded trade carries the price the book would really have charged.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.paper.fills import EMPTY, buy_for_stake, capacity, sell_shares
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}
YES, MKT = "yes1", "m1"


# --------------------------------------------------------------------------- the arithmetic
def test_a_small_order_pays_only_the_best_price():
    fill = buy_for_stake([[0.50, 40], [0.52, 100]], 10.0)
    assert fill.price == 0.50 and fill.levels_used == 1 and fill.complete
    assert fill.shares == 20.0 and fill.slippage == 0.0


def test_a_large_order_walks_the_book_and_pays_the_average():
    # $20 at 0.50, then $30 of the $52 resting at 0.52.
    fill = buy_for_stake([[0.50, 40], [0.52, 100], [0.55, 200]], 50.0)
    assert fill.levels_used == 2 and fill.complete
    assert fill.price == pytest.approx(0.511811, abs=1e-6)
    assert fill.shares == pytest.approx(97.692308, abs=1e-5)
    assert fill.price > fill.top_price and fill.slippage == pytest.approx(0.011811, abs=1e-6)


def test_the_slippage_cap_leaves_the_far_levels_alone():
    book = [[0.50, 40], [0.52, 100], [0.55, 200]]
    assert capacity(book) == 182.0                 # 20 + 52 + 110
    assert capacity(book, 0.02) == 72.0            # 0.55 is 0.05 past the top: out of reach
    fill = buy_for_stake(book, 500.0, 0.02)
    assert fill.stake == 72.0 and not fill.complete and fill.levels_used == 2


def test_an_exit_sells_shares_down_the_bids():
    # 100 shares into 60 at 0.48 then 40 of the 200 at 0.45.
    fill = sell_shares([[0.48, 60], [0.45, 200]], 100.0)
    assert fill.complete and fill.levels_used == 2 and fill.shares == 100.0
    assert fill.stake == pytest.approx(46.8, abs=1e-6)
    assert fill.price == pytest.approx(0.468, abs=1e-6)
    assert fill.price < fill.top_price


def test_selling_more_than_the_book_holds_is_a_partial_fill():
    fill = sell_shares([[0.48, 10]], 100.0)
    assert not fill.complete and fill.shares == 10.0 and fill.stake == pytest.approx(4.8)


def test_an_empty_or_unusable_book_fills_nothing():
    assert buy_for_stake([], 10.0) is EMPTY
    assert sell_shares(None, 10.0) is EMPTY
    assert capacity([]) == 0.0
    # Junk levels are dropped, not guessed at: zero size, a price outside (0, 1], unparseable.
    assert buy_for_stake([[0.5, 0], [1.4, 10], ["x", "y"]], 10.0) is EMPTY


def test_dict_levels_and_string_numbers_are_accepted():
    """The live feed hands over dicts of strings; the store hands over tuples of floats."""
    fill = buy_for_stake([{"price": "0.50", "size": "40"}], 10.0)
    assert fill.price == 0.50 and fill.shares == 20.0


def test_a_nonsense_stake_buys_nothing():
    for bad in (0, -5, float("nan"), float("inf"), None):
        assert buy_for_stake([[0.5, 100]], bad) is EMPTY


# --------------------------------------------------------------------------- the engine
class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    store = MarketStore(tmp_path / "market.db")
    engine = PaperEngine(RiskGovernor(RiskSettings(), RiskState(tmp_path / "risk.db"), lambda: GATE, clock),
                         PaperLedger(tmp_path / "risk.db", 1000.0, clock), store, PaperSettings(), clock=clock)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)", (MKT, "Will it?", (clock() + timedelta(days=30)).isoformat(), at, at))
        db.execute("INSERT OR REPLACE INTO outcomes(token_id, market_id, name) VALUES(?,?,?)", (YES, MKT, "Yes"))
    return store, engine, clock


def book(store, clock, bids, asks):
    """Record a snapshot whose quote is the top of the given book. Levels go in best first."""
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
    with engine._db() as db:
        db.row_factory = __import__("sqlite3").Row
        return dict(db.execute("SELECT * FROM paper_plans WHERE position_id=?", (position_id,)).fetchone())


def test_a_thin_book_is_refused_rather_than_filled_generously(rig):
    store, engine, clock = rig
    book(store, clock, [[0.48, 1000]], [[0.50, 4]])          # $2 resting at the ask
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "REFUSED" and "not enough depth" in r["reason"]


def test_the_entry_price_is_what_walking_the_book_really_cost(rig):
    store, engine, clock = rig
    # $20 wanted: $5 at 0.50, then $15 of the $51 at 0.51.
    book(store, clock, [[0.48, 1000]], [[0.50, 10], [0.51, 100]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED", r
    paid = r["position"]["entry_price"]
    assert paid > 0.50, "a $20 order against $5 at the best ask cannot fill at the best ask"
    assert paid == pytest.approx(0.507468, abs=1e-5)
    row = plan(engine, r["position"]["position_id"])
    assert row["entry_ask"] == pytest.approx(paid)
    assert row["entry_slippage"] == pytest.approx(paid - 0.50, abs=1e-5)
    assert row["entry_levels"] == 2 and row["entry_complete"] == 1
    assert row["entry_requested_stake"] == 20.0


def test_the_stop_hangs_off_the_real_fill_not_the_hoped_for_ask(rig):
    store, engine, clock = rig
    book(store, clock, [[0.48, 1000]], [[0.50, 10], [0.51, 100]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    row = plan(engine, r["position"]["position_id"])
    assert row["stop_price"] == pytest.approx(r["position"]["entry_price"] - 0.05, abs=1e-6)
    assert row["stop_price"] > 0.45, "a stop measured from 0.50 would sit lower than the one we paid for"


def test_the_fill_is_priced_against_the_stake_the_governor_approved(rig):
    """A $500 request walking this book would pay ~0.505. The governor cuts the stake to what the
    bankroll allows, and the smaller order fits entirely at the best ask — so pricing the fill
    against the requested size would charge slippage that was never incurred."""
    store, engine, clock = rig
    book(store, clock, [[0.48, 1000]], [[0.50, 100], [0.51, 100]])   # $50 at the top, $101 in band
    assert buy_for_stake([[0.50, 100.0], [0.51, 100.0]], 500.0, 0.02).price > 0.50
    r = engine.submit(Order("S", MKT, YES, 500.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED", r
    row = plan(engine, r["position"]["position_id"])
    assert row["entry_requested_stake"] == 500.0
    assert r["position"]["stake"] < 500.0, "the governor caps a position this size"
    assert r["position"]["entry_price"] == 0.50
    assert row["entry_levels"] == 1 and row["entry_complete"] == 1


def test_a_stake_the_book_cannot_hold_is_cut_before_the_governor_sees_it(rig):
    """$10 rests within the slippage band; the order asks for $20. Were the request passed through
    whole, the governor would approve $20 and the ledger would book 40 shares against a book that
    held 20 — the same invented liquidity 14A exists to remove."""
    store, engine, clock = rig
    book(store, clock, [[0.48, 1000]], [[0.50, 20]])         # 20 shares at 0.50 = $10
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED", r
    assert r["position"]["stake"] == pytest.approx(10.0), "the stake cannot exceed what rests there"
    assert r["position"]["units"] == pytest.approx(20.0)
    row = plan(engine, r["position"]["position_id"])
    assert row["entry_requested_stake"] == 20.0 and row["entry_complete"] == 1


def test_the_exit_sells_into_the_bids_not_at_the_best_one(rig):
    store, engine, clock = rig
    book(store, clock, [[0.48, 1000]], [[0.50, 1000]])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=1))
    units = r["position"]["units"]
    assert units > 10, units
    # Time is up. Only a few shares rest at 0.60; the rest of the position hits 0.40.
    clock.advance(hours=2)
    book(store, clock, [[0.60, 5], [0.40, 1000]], [[0.62, 1000]])
    assert len(engine.tick()["closed"]) == 1
    closed = engine.ledger.positions("CLOSED")[0]
    assert closed["exit_price"] < 0.60, "selling the whole position cannot all happen at the best bid"
    expected = sell_shares([[0.60, 5.0], [0.40, 1000.0]], units)
    assert closed["exit_price"] == pytest.approx(expected.price, abs=1e-6)
    row = plan(engine, closed["position_id"])
    assert row["exit_levels"] == 2 and row["exit_complete"] == 1
    assert row["exit_slippage"] == pytest.approx(0.60 - expected.price, abs=1e-6)


def test_depth_aware_can_be_turned_off_and_the_old_model_returns(tmp_path):
    """Kept switchable so the two fill models can be compared on the same recorded history. This is
    what every paper result before 14A was measuring."""
    clock = Clock()
    store = MarketStore(tmp_path / "market.db")
    engine = PaperEngine(RiskGovernor(RiskSettings(), RiskState(tmp_path / "risk.db"), lambda: GATE, clock),
                         PaperLedger(tmp_path / "risk.db", 1000.0, clock), store,
                         PaperSettings(depth_aware=False), clock=clock)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT OR REPLACE INTO markets(market_id, question, end_date, first_seen, last_seen)"
                   " VALUES(?,?,?,?,?)", (MKT, "Will it?", (clock() + timedelta(days=30)).isoformat(), at, at))
        db.execute("INSERT OR REPLACE INTO outcomes(token_id, market_id, name) VALUES(?,?,?)", (YES, MKT, "Yes"))
    book(store, clock, [[0.48, 1000]], [[0.50, 4]])          # the book that was refused above
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED"
    assert r["position"]["entry_price"] == 0.50, "without depth the whole order takes the best ask"


def test_a_snapshot_with_no_recorded_book_is_not_tradeable(rig):
    """The scanner records the top levels on every snapshot, so a snapshot with none is a market
    whose book was empty. Refusing is the point: an unknown book cannot be filled against."""
    store, engine, clock = rig
    clock.advance(seconds=1)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask, mid,"
                   " spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,0.48,0.50,0.49,0.02,'[]',1)",
                   (at, YES, MKT))
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "REFUSED" and "not enough depth" in r["reason"]


def test_the_newest_book_is_the_one_used(rig):
    """Two snapshots can share a timestamp. The quote comes from the later row, so the book must
    too, or a trade is priced against a book that does not belong to it."""
    store, engine, clock = rig
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'scan')", (at,))
        for bid, ask, size in ((0.20, 0.22, 2.0), (0.48, 0.50, 1000.0)):
            cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid, best_ask,"
                             " mid, spread, flags, usable) VALUES((SELECT MAX(run_id) FROM runs),?,?,?,?,?,?,?,'[]',1)",
                             (at, YES, MKT, bid, ask, (bid + ask) / 2, ask - bid))
            db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                           " VALUES(?,?,?,?,?,?)",
                           [(cur.lastrowid, "bid", 0, bid, size, at), (cur.lastrowid, "ask", 0, ask, size, at)])
    r = engine.submit(Order("S", MKT, YES, 20.0, stop=0.05, target=None, max_hold_hours=24))
    assert r["status"] == "OPENED", r
    assert r["position"]["entry_price"] == 0.50, "the stale 0.22 book must not price this fill"
