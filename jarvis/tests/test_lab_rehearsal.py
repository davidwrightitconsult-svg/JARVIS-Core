"""Phase 17: calibrating the instrument.

Everything up to Phase 16 built an apparatus for measuring edge. Its arithmetic is unit-tested to
the penny, which is a different claim from "it can measure": a thermometer with perfect arithmetic
and a bad sensor reads 20 degrees in a furnace.

So these plant strategies whose informational content is known BY CONSTRUCTION, run them through
the real store, the real Scorer, the real edge report and the real Executor, and check the
instrument reaches the verdict the construction requires. The two failure modes that would make
every number this platform produces worthless are opposite, so one harness has to catch both:

    a FALSE POSITIVE on noise     - COINFLIP credited with information it does not have
    a FALSE NEGATIVE on real edge - ORACLE dismissed

None of this is evidence that a real strategy works. ORACLE cheats by construction.
"""

import pathlib

import pytest

from jarvis.lab.rehearsal import (
    ANTI, COINFLIP, ORACLE, PLANTED, WEAK, Rehearsal, SimSettings, check, render,
)
from jarvis.market.store import MarketStore
from jarvis.paper.costs import CostSettings


def run(tmp_path, name="m.db", **kw):
    store = MarketStore(pathlib.Path(tmp_path) / name)
    out = Rehearsal(settings=SimSettings(**kw)).run(store)
    return store, out


@pytest.fixture(scope="module")
def rehearsed(tmp_path_factory):
    """One default run, shared: it takes a couple of seconds and nothing here mutates it."""
    store = MarketStore(tmp_path_factory.mktemp("rehearsal") / "m.db")
    return store, Rehearsal().run(store)


# --------------------------------------------------------------------------- the headline
def test_the_instrument_is_calibrated(rehearsed):
    _store, out = rehearsed
    verdicts = check(out)
    assert verdicts["calibrated"], render(out, verdicts)


def test_every_planted_signal_is_scored(rehearsed):
    """`due_signals` returns at most 500 rows a call. One pass silently scores a fraction of a
    large plant and truncates each strategy unevenly, which is how the first run produced an
    ORACLE with 24 of its 145 signals."""
    _store, out = rehearsed
    assert out["scoring"]["scored"] == sum(out["planted"].values())
    assert out["scoring"]["by_status"] == {"SCORED": out["scoring"]["scored"]}
    assert out["scoring"]["passes"] > 1, "and it took more than one pass to get there"


def test_the_planted_strategies_get_the_verdicts_their_construction_requires(rehearsed):
    _store, out = rehearsed
    got = {name: out["edge"]["strategies"][name]["verdict"] for name in PLANTED}
    assert got == {ORACLE: "MAKES_MONEY",
                   WEAK: "KNOWS_SOMETHING_BUT_COSTS_EAT_IT",
                   COINFLIP: "NEGATIVE_AFTER_COSTS",
                   ANTI: "WORSE_THAN_A_COIN_FLIP"}


# --------------------------------------------------------------------------- the two failure modes
def test_pure_noise_is_not_credited_with_information(rehearsed):
    """The false positive that would invalidate everything. A thousand coin flips, and the
    information interval must still straddle zero."""
    _store, out = rehearsed
    row = out["edge"]["strategies"][COINFLIP]
    assert row["scored"] > 900, "with a small sample this proves little"
    info = row["edge_vs_coinflip"]
    assert info["beats_zero"] is False
    assert info["ci95"][0] < 0 < info["ci95"][1], info
    assert abs(info["mean"]) < 0.01, "a coin flip's information is zero, give or take noise"


def test_a_real_edge_is_found_and_not_dismissed(rehearsed):
    """The false negative. ORACLE is selective and sees the future, so the money interval must be
    entirely above zero -- if the instrument cannot see this, it would veto a real edge."""
    _store, out = rehearsed
    row = out["edge"]["strategies"][ORACLE]
    money = row["net_after_fees"]
    assert money["beats_zero"] and money["ci95"][0] > 0, money
    assert row["edge_vs_coinflip"]["beats_zero"]


def test_information_and_profit_are_told_apart(rehearsed):
    """WEAK and ORACLE both see the future. WEAK trades only the moves too small to pay for
    themselves. The instrument must separate 'knows something' from 'makes money'."""
    _store, out = rehearsed
    weak = out["edge"]["strategies"][WEAK]
    assert weak["edge_vs_coinflip"]["beats_zero"], "the information is real"
    assert weak["net_after_fees"]["ci95"][1] < 0, "and it still loses money"


def test_a_strategy_that_is_wrong_on_purpose_is_identified(rehearsed):
    _store, out = rehearsed
    anti = out["edge"]["strategies"][ANTI]
    assert anti["edge_vs_coinflip"]["ci95"][1] < 0
    assert anti["mirror_after_fees"]["mean"] > anti["net_after_fees"]["mean"]


# --------------------------------------------------------------------------- what the sim shows
def test_the_coinflip_baseline_is_about_the_spread(rehearsed):
    """A 0.02 spread crossed twice is 0.02 a share, plus the fee. The measured baseline has to
    land there or the baseline is not measuring what it claims."""
    _store, out = rehearsed
    row = out["edge"]["strategies"][COINFLIP]
    fee = row["avg_fee_per_share"]
    assert row["coinflip_after_fees"]["mean"] == pytest.approx(-(0.02 + fee), abs=0.003)


def test_fees_are_most_of_what_separates_knowing_from_earning(tmp_path):
    """WEAK's information is worth less than the round trip. Remove the fee and the gap narrows by
    about the fee -- which is the whole argument for 14B existing before any of this."""
    _s1, charged = run(tmp_path, "a.db")
    _s2, free = run(tmp_path, "b.db", category_question="Will Russia and Ukraine sign a ceasefire?")
    c = charged["edge"]["strategies"][WEAK]
    f = free["edge"]["strategies"][WEAK]
    assert c["avg_fee_per_share"] > 0.015 and f["avg_fee_per_share"] == 0.0
    assert f["net_after_fees"]["mean"] > c["net_after_fees"]["mean"]
    assert f["net_after_fees"]["mean"] - c["net_after_fees"]["mean"] == pytest.approx(
        c["avg_fee_per_share"], abs=0.004)


def test_a_wider_spread_raises_the_bar_for_everyone(tmp_path):
    _s1, tight = run(tmp_path, "t.db", spread=0.01)
    _s2, wide = run(tmp_path, "w.db", spread=0.06)
    for name in (ORACLE, COINFLIP):
        t = tight["edge"]["strategies"][name]["coinflip_after_fees"]["mean"]
        w = wide["edge"]["strategies"][name]["coinflip_after_fees"]["mean"]
        assert w < t, f"{name}: a wider market costs more to participate in"
    assert wide["edge"]["strategies"][ORACLE]["net_after_fees"]["mean"] < \
        tight["edge"]["strategies"][ORACLE]["net_after_fees"]["mean"]


# --------------------------------------------------------------------------- the executor loop
def test_only_the_strategy_that_makes_money_is_allowed_to_trade(rehearsed):
    """The whole chain: measure, judge, permit. The gate approves all four; the executor, reading
    the real edge report, lets exactly one of them open a position."""
    _store, out = rehearsed
    decisions = out["execution"]
    assert decisions[ORACLE]["allowed"] is True and decisions[ORACLE]["max_stake"] == 20.0
    for name in (WEAK, COINFLIP, ANTI):
        assert decisions[name]["allowed"] is False, name
        assert "losing money now" in decisions[name]["reason"], name


# --------------------------------------------------------------------------- integrity
def test_the_run_is_reproducible(tmp_path):
    _s1, a = run(tmp_path, "a.db")
    _s2, b = run(tmp_path, "b.db")
    assert a["planted"] == b["planted"]
    assert ({k: v["verdict"] for k, v in a["edge"]["strategies"].items()}
            == {k: v["verdict"] for k, v in b["edge"]["strategies"].items()})


def test_calibration_is_not_a_property_of_one_lucky_seed(tmp_path):
    for seed in (1, 99, 2026):
        _store, out = run(tmp_path, f"s{seed}.db", seed=seed)
        verdicts = check(out)
        assert verdicts["calibrated"], f"seed {seed}\n{render(out, verdicts)}"


def test_it_drives_the_real_store_not_a_stub(rehearsed):
    """If this ran against a mock, calibration would prove nothing about the real pipeline."""
    store, out = rehearsed
    stats = store.stats(days=3650)
    assert stats["counts"]["snapshots"] == out["settings"]["markets"] * out["settings"]["scans"] * 2
    assert stats["counts"]["book_levels"] > 0, "real recorded depth, as 14A requires"
    assert len(store.markets(limit=500)) == out["settings"]["markets"]
    assert out["edge"]["strategies"][ORACLE]["days"] >= 1


def test_the_rendering_says_what_it_does_and_does_not_prove(rehearsed):
    _store, out = rehearsed
    text = render(out, check(out))
    assert "CALIBRATED" in text
    assert "says NOTHING about" in text, "the caveat is part of the output, not the commit message"
