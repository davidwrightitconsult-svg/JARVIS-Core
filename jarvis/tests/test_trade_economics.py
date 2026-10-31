"""Phase 18: the economy of a trade.

Three findings drove this phase:

  - The round trip (spread once + the fee on both legs) is what every strategy must beat, and it
    varies by more than 5x across the markets FRIDAY records. The same prediction is profitable in
    one market and a loss in another.
  - `BollingerResting`'s minimum target was 1c, below the cost of the trade it authorised.
  - `rest()` fills at its own limit price, which is a MAKER fill paying no taker fee. That was true
    by accident, documented as the opposite, and not bounded by any observable liquidity.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.lab.economics import ECONOMICS_VERSION, render, report
from jarvis.market.models import Market, Outcome, SnapshotRow
from jarvis.market.store import MarketStore
from jarvis.paper import Order, PaperEngine, PaperSettings
from jarvis.paper.costs import CostSettings, round_trip
from jarvis.risk import PaperLedger, RiskGovernor, RiskSettings, RiskState

GATE = {"approved_for_paper": ["S"], "forward_test": [], "live_approved": []}

# question -> (fee category, bid, ask). Spreads and prices chosen to span the cost range.
UNIVERSE = {
    "cheap": ("Will Russia and Ukraine sign a ceasefire?", "geopolitics", 0.92, 0.94),
    "mid": ("Will the index close above 100 on Friday?", "economy", 0.48, 0.50),
    # 5c spread: exactly at the engine's existing max_spread, so it reaches the round-trip gate
    # rather than being refused before it. 5c spread + 3.5c of fees = 17% of the capital.
    "dear": ("Will Bitcoin close above $100k?", "crypto", 0.45, 0.50),
}


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc) - timedelta(hours=1)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def universe(tmp_path, clock=None, size=4000.0):
    """One run covering three cost regimes, so `latest_rows` sees all of them."""
    clock = clock or Clock()
    store = MarketStore(tmp_path / "market.db")
    at = clock().isoformat(timespec="seconds")
    end = (clock() + timedelta(days=30)).isoformat(timespec="seconds")
    markets, rows = [], []
    for key, (question, _cat, bid, ask) in UNIVERSE.items():
        markets.append(Market(market_id=key, question=question, end_date=end, neg_risk=False,
                              outcomes=[Outcome(name=n, token_id=f"{key}-{n.lower()}")
                                        for n in ("Yes", "No")]))
        for name, (b, a) in (("Yes", (bid, ask)), ("No", (round(1 - ask, 4), round(1 - bid, 4)))):
            levels_b = [(round(b - i * 0.01, 4), size) for i in range(3) if b - i * 0.01 > 0]
            levels_a = [(round(a + i * 0.01, 4), size) for i in range(3) if a + i * 0.01 < 1]
            rows.append(SnapshotRow(market_id=key, question=question, outcome=name,
                                    token_id=f"{key}-{name.lower()}", best_bid=b, best_ask=a,
                                    mid=round((b + a) / 2, 4), spread=round(a - b, 4),
                                    last_trade_price=None, depth_usd=size * b, gamma_liquidity=None,
                                    volume_24h=None, end_date=end, flags=[], usable=True,
                                    observed_at=at, top_bids=levels_b, top_asks=levels_a))
    store.record(markets, rows, {"observed_at": at, "markets": len(markets), "tokens": len(rows),
                                 "usable_rows": len(rows)}, source="economics")
    return store, clock


# --------------------------------------------------------------------------- the report
@pytest.fixture
def econ(tmp_path):
    store, _clock = universe(tmp_path)
    return report(store)


def test_the_report_prices_every_recorded_token(econ):
    assert econ["economics_version"] == ECONOMICS_VERSION
    assert econ["tokens"] == 6
    assert set(econ["by_category"]) == {"geopolitics", "economy", "crypto"}


def test_the_round_trip_varies_several_fold_across_the_same_universe(econ):
    """The whole reason this report exists: the cost is not a constant to be assumed away."""
    best = econ["overall"]["best_fraction_of_cost"]
    worst = econ["overall"]["worst_fraction_of_cost"]
    assert worst > 3 * best, (best, worst)


def test_the_cheapest_market_is_the_zero_fee_one_at_a_high_price(econ):
    top = econ["cheapest"][0]
    assert top["category"] == "geopolitics" and top["ask"] >= 0.90
    assert top["round_trip"]["entry_fee"] == 0.0 and top["round_trip"]["exit_fee"] == 0.0
    assert top["round_trip"]["total"] == pytest.approx(0.02), "the spread is the whole cost"


def test_the_dearest_market_is_a_cheap_contract_not_a_high_fee_one(econ):
    """Not what I expected, and more useful: the worst market here is the NO leg of the zero-fee
    geopolitics contract, priced at 0.06/0.08. A 2c spread is a quarter of an 8c contract. The fee
    rate is only one of three drivers, and on cheap contracts the spread dominates everything."""
    worst = econ["dearest"][0]
    assert worst["category"] == "geopolitics" and worst["ask"] <= 0.10
    assert worst["round_trip"]["entry_fee"] == 0.0 and worst["round_trip"]["exit_fee"] == 0.0
    assert worst["round_trip"]["fraction_of_cost"] == pytest.approx(0.25, abs=0.01)
    crypto = next(p for p in econ["dearest"] if p["category"] == "crypto")
    assert crypto["round_trip"]["fraction_of_cost"] < worst["round_trip"]["fraction_of_cost"]


def test_each_category_reports_what_an_edge_must_beat_there(econ):
    cats = econ["by_category"]
    assert cats["geopolitics"]["rate"] == 0.0 and cats["crypto"]["rate"] == 0.07
    assert cats["crypto"]["breakeven_edge_cents"] > cats["geopolitics"]["breakeven_edge_cents"]
    assert cats["economy"]["by_band"], "and split by price band inside the category"


def test_resting_at_the_bid_is_reported_as_the_saving_it_is(econ):
    o = econ["overall"]
    assert o["median_round_trip_as_maker"] < o["median_round_trip"]
    assert o["median_saved_by_resting"] > 0


def test_an_empty_store_reports_nothing_rather_than_failing(tmp_path):
    out = report(MarketStore(tmp_path / "empty.db"))
    assert out["tokens"] == 0 and out["by_category"] == {}
    assert "no recorded two-sided quotes yet" in render(out)


def test_the_rendering_names_the_trade_off_it_is_describing(econ):
    text = render(econ)
    assert "CHEAPEST MARKETS TO TRADE" in text and "DEAREST" in text
    assert "profitable in one of these markets and a loss in another" in text


# --------------------------------------------------------------------------- the entry gate
def rig(tmp_path, clock=None, **kw):
    clock = clock or Clock()
    store, _ = universe(tmp_path, clock)
    engine = PaperEngine(RiskGovernor(RiskSettings(), RiskState(tmp_path / "risk.db"), lambda: GATE, clock),
                         PaperLedger(tmp_path / "risk.db", 1000.0, clock), store,
                         PaperSettings(**kw), clock=clock)
    return store, engine, clock


def order(market="mid", outcome="yes", stake=20.0, **kw):
    return Order("S", market, f"{market}-{outcome}", stake,
                 stop=kw.pop("stop", 0.05), target=kw.pop("target", None),
                 max_hold_hours=kw.pop("max_hold_hours", 24.0))


def test_a_market_whose_round_trip_eats_the_capital_is_refused(tmp_path):
    """crypto at 0.44/0.50 costs 17% of the capital committed to get in and out again. An edge has
    to beat that before it is an edge, so the attempt is not worth making."""
    _store, engine, _clock = rig(tmp_path, max_round_trip_fraction=0.15)
    r = engine.submit(order("dear"))
    assert r["status"] == "REFUSED"
    assert "the round trip costs" in r["reason"] and "17.0% of" in r["reason"]


def test_the_same_order_is_accepted_in_a_cheap_market(tmp_path):
    _store, engine, _clock = rig(tmp_path, max_round_trip_fraction=0.15)
    assert engine.submit(order("cheap"))["status"] == "OPENED"
    assert engine.submit(order("mid"))["status"] == "OPENED", "9% is inside the limit"


def test_the_gate_can_be_tightened_to_refuse_mid_priced_markets(tmp_path):
    _store, engine, _clock = rig(tmp_path, max_round_trip_fraction=0.05)
    assert engine.submit(order("cheap"))["status"] == "OPENED", "4% survives a 5% limit"
    assert engine.submit(order("mid"))["status"] == "REFUSED"


def test_the_gate_can_be_turned_off(tmp_path):
    _store, engine, _clock = rig(tmp_path, max_round_trip_fraction=None)
    assert engine.submit(order("dear"))["status"] == "OPENED"


def test_the_refusal_names_the_arithmetic_so_it_can_be_checked(tmp_path):
    _store, engine, _clock = rig(tmp_path, max_round_trip_fraction=0.05)
    reason = engine.submit(order("mid"))["reason"]
    rt = round_trip(CostSettings(), 0.48, 0.50, "economy")
    assert f"{rt.total:.4f}" in reason and "economy" in reason
    assert f"spread {rt.spread:.3f}" in reason


# --------------------------------------------------------------------------- the maker path
def test_a_resting_order_fills_at_its_own_limit_and_pays_no_taker_fee(tmp_path):
    """18A: the order rested at the limit and someone crossed into it. That is a maker fill: the
    price is the limit, not the market's now-better ask, and no taker fee is charged."""
    store, engine, clock = rig(tmp_path)
    r = engine.rest(order("mid", target=0.05), limit_price=0.47, ttl_minutes=120)
    assert r["status"] == "RESTING", r

    clock.advance(minutes=20)                      # the ask falls through our bid
    store2, _ = universe(tmp_path / "later", clock)   # (fresh quotes recorded below instead)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'economics')", (at,))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid,"
                         " best_ask, mid, spread, flags, usable)"
                         " VALUES((SELECT MAX(run_id) FROM runs),?,?,?,0.44,0.46,0.45,0.02,'[]',1)",
                         (at, "mid-yes", "mid"))
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)",
                       [(cur.lastrowid, "bid", 0, 0.44, 4000.0, at),
                        (cur.lastrowid, "ask", 0, 0.46, 4000.0, at)])
    out = engine.tick()
    assert len(out["filled"]) == 1, out
    position = engine.ledger.positions("OPEN")[0]
    import sqlite3
    with engine._db() as db:
        db.row_factory = sqlite3.Row
        plan = dict(db.execute("SELECT * FROM paper_plans WHERE position_id=?",
                               (position["position_id"],)).fetchone())
    assert plan["maker_entry"] == 1
    assert plan["entry_fill_price"] == 0.47, "filled at OUR price, not the market's 0.46"
    assert plan["entry_fee_usd"] == 0.0, "a maker pays no taker fee"
    assert position["entry_price"] == 0.47, "and no fee is folded into the booked price"
    assert plan["fee_category"] == "economy"


def test_a_resting_fill_is_bounded_by_observable_liquidity(tmp_path):
    """A resting order can only be filled by someone willing to sell to it. Filling $500 against
    $2 of observable sell interest is the pre-14A generosity, which this path still had."""
    store, engine, clock = rig(tmp_path)
    assert engine.rest(order("mid", stake=500.0, target=0.05), 0.47, 120)["status"] == "RESTING"
    clock.advance(minutes=20)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'economics')", (at,))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid,"
                         " best_ask, mid, spread, flags, usable)"
                         " VALUES((SELECT MAX(run_id) FROM runs),?,?,?,0.44,0.46,0.45,0.02,'[]',1)",
                         (at, "mid-yes", "mid"))
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)",
                       [(cur.lastrowid, "bid", 0, 0.44, 4000.0, at),
                        (cur.lastrowid, "ask", 0, 0.46, 40.0, at)])    # $18.40 at or under 0.47
    engine.tick()
    opened = engine.ledger.positions("OPEN")
    assert len(opened) == 1
    assert opened[0]["stake"] < 500.0
    assert opened[0]["stake"] == pytest.approx(18.4, abs=0.1), "cut to what rests at or under 0.47"


def test_a_resting_order_with_no_one_to_sell_to_is_refused(tmp_path):
    store, engine, clock = rig(tmp_path)
    assert engine.rest(order("mid", target=0.05), 0.47, 120)["status"] == "RESTING"
    clock.advance(minutes=20)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'economics')", (at,))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid,"
                         " best_ask, mid, spread, flags, usable)"
                         " VALUES((SELECT MAX(run_id) FROM runs),?,?,?,0.44,0.46,0.45,0.02,'[]',1)",
                         (at, "mid-yes", "mid"))
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)",
                       [(cur.lastrowid, "bid", 0, 0.44, 4000.0, at),
                        (cur.lastrowid, "ask", 0, 0.46, 2.0, at)])     # $0.92: under the $5 floor
    engine.tick()
    assert engine.ledger.positions("OPEN") == []
    resting = engine.resting("REFUSED", 10)
    assert resting and "only $0.92 rests at or under" in resting[0]["note"]


def test_a_rebate_shows_up_as_a_better_booked_price(tmp_path):
    """Off by default, because crediting income FRIDAY may not earn is the generous-fill mistake
    again. With it on, a maker entry books BELOW its own limit."""
    store, engine, clock = rig(tmp_path, costs=CostSettings(maker_rebate_share=0.25))
    assert engine.rest(order("mid", target=0.05), 0.47, 120)["status"] == "RESTING"
    clock.advance(minutes=20)
    at = clock().isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("INSERT INTO runs(observed_at, source) VALUES(?, 'economics')", (at,))
        cur = db.execute("INSERT INTO snapshots(run_id, observed_at, token_id, market_id, best_bid,"
                         " best_ask, mid, spread, flags, usable)"
                         " VALUES((SELECT MAX(run_id) FROM runs),?,?,?,0.44,0.46,0.45,0.02,'[]',1)",
                         (at, "mid-yes", "mid"))
        db.executemany("INSERT INTO book_levels(snapshot_id, side, level, price, size, observed_at)"
                       " VALUES(?,?,?,?,?,?)",
                       [(cur.lastrowid, "bid", 0, 0.44, 4000.0, at),
                        (cur.lastrowid, "ask", 0, 0.46, 4000.0, at)])
    engine.tick()
    position = engine.ledger.positions("OPEN")[0]
    assert position["entry_price"] < 0.47, "the rebate is negative cost"
