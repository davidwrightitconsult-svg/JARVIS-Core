"""Phase 16A: the edge report. After fees, and against a coin flip on the same signals.

Two questions the system previously conflated into one:
    net_after_fees > 0            does this MAKE MONEY
    net_after_fees > coin_flip    does this KNOW ANYTHING

The coin flip is not zero. For any signal the two directions are mirror images at the same two
moments, and they sum to -(entry_spread + exit_spread) -- so a position taken with no information
loses half the two spreads, every time.
"""

import pytest

from jarvis.lab.edge import (
    ANTI, KNOWS_SOMETHING, LOSING, MAKES_MONEY, NOTHING, THIN, UNCLEAR,
    enrich, fee_category, mirror_net, prices_of, render, report, round_trip_fee,
)
from jarvis.paper.costs import CostSettings

FREE = CostSettings(enabled=False)
CHARGED = CostSettings()


def sig(direction="UP", eb=0.48, ea=0.52, xb=0.55, xa=0.59, question="Will it rain?", **kw):
    """One scored signal. `net` follows the 7C convention so the report is fed what it would be."""
    missing = None in (eb, ea, xb, xa)
    net = None if missing else ((xb - ea) if direction == "UP" else (eb - xa))
    row = {"signal_id": kw.pop("signal_id", 1), "strategy": "S", "strategy_version": "v1",
           "observed_at": kw.pop("observed_at", "2026-10-01T00:00:00+00:00"), "market_id": "m1",
           "token_id": "t1", "direction": direction, "status": "SCORED", "question": question,
           "best_bid": eb, "best_ask": ea, "exit_bid": xb, "exit_ask": xa,
           "mid": (eb + ea) / 2, "net": None if net is None else round(net, 6),
           "resolution_net": None}
    row.update(kw)
    return row


# --------------------------------------------------------------------------- the mirror
def test_the_mirror_is_the_opposite_direction_at_the_same_two_moments():
    # UP bought at 0.52 and sold at 0.55. DOWN would have bought at 1-0.48 and sold at 1-0.59.
    assert mirror_net(sig("UP")) == pytest.approx(0.48 - 0.59)
    assert mirror_net(sig("DOWN")) == pytest.approx(0.55 - 0.52)


def test_the_two_directions_sum_to_minus_both_spreads():
    """The identity the whole report rests on. Entry spread 0.04, exit spread 0.04."""
    up = sig("UP")
    assert up["net"] + mirror_net(up) == pytest.approx(-(0.04 + 0.04), abs=1e-9)


def test_a_coin_flip_loses_half_the_spreads():
    e = enrich(sig("UP"), FREE)
    assert e["coinflip_after_fees"] == pytest.approx(-0.04)
    assert e["edge_vs_coinflip"] == pytest.approx(e["net_after_fees"] + 0.04)


def test_the_coinflip_baseline_is_worse_in_a_wider_market():
    """This is why `edge_vs_coinflip` is not a restatement of `net`: the bar moves per signal."""
    tight = enrich(sig("UP", eb=0.49, ea=0.51, xb=0.55, xa=0.57), FREE)
    wide = enrich(sig("UP", eb=0.40, ea=0.60, xb=0.55, xa=0.75), FREE)
    assert tight["coinflip_after_fees"] > wide["coinflip_after_fees"]
    assert wide["coinflip_after_fees"] == pytest.approx(-0.20)


def test_a_row_with_no_exit_book_carries_no_comparison():
    assert enrich(sig("UP", xb=None), FREE) is None
    assert enrich({**sig(), "status": "NO_EXIT_DATA"}, FREE) is None
    assert enrich({**sig(), "net": None}, FREE) is None


# --------------------------------------------------------------------------- fees
def test_what_was_bought_is_what_is_charged():
    """UP buys the token at its ask. DOWN buys the other outcome, which costs 1 - this one's bid."""
    assert prices_of(sig("UP")) == (0.52, 0.55)
    assert prices_of(sig("DOWN")) == (pytest.approx(0.52), pytest.approx(0.41))


def test_the_fee_is_the_same_model_the_engine_trades_with():
    # "Will it rain?" -> category "other" -> 0.05. Both legs: 0.05*(0.52*0.48 + 0.55*0.45).
    assert fee_category(sig()) == "other"
    assert round_trip_fee(sig("UP"), CHARGED) == pytest.approx(
        0.05 * (0.52 * 0.48) + 0.05 * (0.55 * 0.45), abs=1e-8)


def test_the_category_changes_the_fee_and_can_change_the_answer():
    crypto = sig("UP", question="Will Bitcoin close above $100k?")
    free_cat = sig("UP", question="Will Russia and Ukraine sign a ceasefire?")
    assert fee_category(crypto) == "crypto" and fee_category(free_cat) == "geopolitics"
    assert round_trip_fee(crypto, CHARGED) > round_trip_fee(sig(), CHARGED)
    assert round_trip_fee(free_cat, CHARGED) == 0.0


def test_fees_turn_a_three_cent_gain_into_half_a_cent():
    """The number that makes this phase worth building: the 7C scorecard reports 0.03 and the
    engine would have booked 0.005."""
    gross = enrich(sig("UP"), FREE)
    net = enrich(sig("UP"), CHARGED)
    assert gross["net_after_fees"] == pytest.approx(0.03)
    assert net["net_after_fees"] == pytest.approx(0.005145, abs=1e-6)
    assert net["net"] == pytest.approx(0.03), "the gross is still reported, for comparison"


def test_an_unclassified_market_is_charged_the_worst_rate():
    unknown = sig("UP", question=None)
    assert fee_category(unknown) == "unknown"
    assert round_trip_fee(unknown, CHARGED) == pytest.approx(
        0.07 * (0.52 * 0.48) + 0.07 * (0.55 * 0.45), abs=1e-8)


# --------------------------------------------------------------------------- the verdicts
def many(n, **kw):
    return [sig(signal_id=i, observed_at=f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00", **kw)
            for i in range(n)]


def test_a_thin_sample_says_so_rather_than_guessing():
    r = report(many(5), FREE)["strategies"]["S"]
    assert r["verdict"] == THIN and "25 more" in r["reason"]


def test_a_profitable_strategy_makes_money():
    r = report(many(60), FREE)["strategies"]["S"]
    assert r["verdict"] == MAKES_MONEY
    assert r["net_after_fees"]["beats_zero"] and r["net_after_fees"]["mean"] == pytest.approx(0.03)
    assert r["edge_vs_coinflip"]["mean"] == pytest.approx(0.07)


def test_the_two_bars_can_disagree_and_that_is_the_point():
    """A 1c gross gain in a 0.05 category: it beats the coin flip handsomely (the spread alone
    costs 4c) but loses money after fees. Real information, eaten by costs."""
    rows = many(60, xb=0.53, xa=0.57)                    # gross +0.01
    r = report(rows, CHARGED)["strategies"]["S"]
    assert r["net_after_fees"]["mean"] < 0
    assert r["edge_vs_coinflip"]["beats_zero"]
    assert r["verdict"] == KNOWS_SOMETHING
    assert "the costs are eating it" in r["reason"]


def test_a_strategy_reliably_worse_than_random_is_reported_as_such():
    """-0.12 a share where a coin flip loses 0.04. That is not "no information", it is inverted
    information: the mirror of these same signals made +0.04. Reported as a lead, not a strategy."""
    r = report(many(60, xb=0.40, xa=0.44), FREE)["strategies"]["S"]
    assert r["verdict"] == ANTI
    assert r["net_after_fees"]["mean"] == pytest.approx(-0.12)
    assert r["coinflip_after_fees"]["mean"] == pytest.approx(-0.04)
    assert r["mirror_after_fees"]["mean"] == pytest.approx(0.04)
    assert "a lead to test, not a strategy to run" in r["reason"]


def test_losing_money_with_an_inconclusive_edge_is_its_own_verdict():
    """The narrow case LOSING is for: the coin-flip comparison cannot rule out information, but
    after fees the money interval is entirely below zero."""
    rows = []
    for i in range(80):
        # Nets of -0.02 and -0.06 against a 0.04 coin-flip cost: the edge averages zero with
        # enough spread that the interval straddles it, while the 0.07 crypto fee puts the money
        # interval clearly below zero.
        xb = 0.50 if i % 2 else 0.46
        rows.append(sig(signal_id=i, observed_at=f"2026-10-{(i % 28) + 1:02d}T00:00:00+00:00",
                        xb=xb, xa=round(xb + 0.04, 2),
                        question="Will Bitcoin close above $100k?"))
    r = report(rows, CHARGED)["strategies"]["S"]
    assert r["net_after_fees"]["ci95"][1] < 0, r["net_after_fees"]
    assert not r["edge_vs_coinflip"]["beats_zero"]
    assert r["edge_vs_coinflip"]["ci95"][1] > 0, "the edge is inconclusive, not ruled out"
    assert r["verdict"] == LOSING


def test_a_strategy_that_only_matches_a_coin_flip_has_no_edge():
    """Exactly the coin flip: the price did not move at all, so both directions lose half the
    spreads and the strategy's selection added nothing."""
    rows = many(60, xb=0.48, xa=0.52)
    r = report(rows, FREE)["strategies"]["S"]
    assert r["edge_vs_coinflip"]["mean"] == pytest.approx(0.0, abs=1e-9)
    assert not r["edge_vs_coinflip"]["beats_zero"]
    # It also lost money (the half-spread), but NO_EDGE names the cause rather than the symptom:
    # losing money can be fixed by trading cheaper markets, knowing nothing cannot.
    assert r["verdict"] == NOTHING
    assert r["net_after_fees"]["ci95"][1] < 0


def test_a_positive_mean_with_an_interval_straddling_zero_is_not_an_edge():
    """One lucky signal in a noisy sample. A positive mean is not a finding."""
    rows = [sig(signal_id=0, xb=3.0, xa=3.04)] + many(40, xb=0.47, xa=0.51)
    for i, row in enumerate(rows):
        row["signal_id"] = i
    r = report(rows, FREE)["strategies"]["S"]
    assert r["net_after_fees"]["mean"] > 0
    assert not r["net_after_fees"]["beats_zero"]
    assert r["verdict"] == UNCLEAR


# --------------------------------------------------------------------------- integrity
def test_signals_from_two_versions_are_not_one_strategy():
    """Pooling two versions produces a confidence interval about nothing. The report says so."""
    rows = many(30) + [{**s, "strategy_version": "v2"} for s in many(30)]
    for i, row in enumerate(rows):
        row["signal_id"] = i
    r = report(rows, FREE)["strategies"]["S"]
    assert r["mixed_versions"] is True and r["versions"] == ["v1", "v2"]
    assert "MIXED VERSIONS" in render(report(rows, FREE))


def test_one_version_is_not_flagged():
    assert report(many(30), FREE)["strategies"]["S"]["mixed_versions"] is False


def test_the_same_prediction_splits_by_fee_category():
    """Identical predictions, different categories: profitable in geopolitics, losing in crypto."""
    rows = (many(40, question="Will Russia and Ukraine sign a ceasefire?", xb=0.53, xa=0.57)
            + many(40, question="Will Bitcoin close above $100k?", xb=0.53, xa=0.57))
    for i, row in enumerate(rows):
        row["signal_id"] = i
    cats = report(rows, CHARGED)["strategies"]["S"]["by_category"]
    assert cats["geopolitics"]["avg_fee_per_share"] == 0.0
    assert cats["crypto"]["avg_fee_per_share"] > 0.03
    assert cats["geopolitics"]["net_after_fees"]["mean"] > cats["crypto"]["net_after_fees"]["mean"]
    assert cats["geopolitics"]["net_after_fees"]["mean"] > 0 > cats["crypto"]["net_after_fees"]["mean"]


def test_strategies_are_reported_separately():
    rows = many(40) + [{**s, "strategy": "T"} for s in many(40, xb=0.40, xa=0.44)]
    for i, row in enumerate(rows):
        row["signal_id"] = i
    out = report(rows, FREE)
    assert set(out["strategies"]) == {"S", "T"}
    assert out["strategies"]["S"]["verdict"] == MAKES_MONEY
    assert out["strategies"]["T"]["verdict"] == ANTI


def test_an_empty_record_reports_nothing_rather_than_failing():
    out = report([], FREE)
    assert out["strategies"] == {} and out["scored_signals"] == 0
    assert "no scored signals" in render(out)
