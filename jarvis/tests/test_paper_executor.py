"""Phase 16B: the execution authority. May this strategy open a trade right now, at this size?

There were two authorities and a gap between them:

    the 9C gate         may this strategy EVER trade?   slow, once, on 100 signals over 7+ days
    [missing]           may it trade NOW, at THIS size?
    the Risk Governor   how much, given the portfolio?   per order, on exposure and drawdown

A strategy could pass the gate in October, decay through November, and keep trading on a promotion
it had stopped deserving. The Executor is the missing middle, and it can only ever say no.
"""

from datetime import datetime, timedelta, timezone

import pytest

from jarvis.paper.executor import (
    APPROVED_FOR_PAPER, EXECUTOR_VERSION, FORWARD_TEST, ExecutionSettings, Executor,
)

S = "RESEARCH_FORECAST"
GATE = {"approved_for_paper": [S], "forward_test": [], "live_approved": []}
FORWARD_GATE = {"approved_for_paper": [], "forward_test": [S], "live_approved": []}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def edge(verdict="MAKES_MONEY", scored=200, ci=(0.01, 0.03), versions=("v1",), mixed=False):
    return {"strategies": {S: {"scored": scored, "verdict": verdict, "versions": list(versions),
                               "mixed_versions": mixed,
                               "net_after_fees": {"n": scored, "mean": sum(ci) / 2,
                                                  "ci95": list(ci), "beats_zero": ci[0] > 0}}}}


def ex(gate=GATE, edge_report=None, capacity=None, clock=None, **kw):
    settings = ExecutionSettings(**{"enabled": True, **kw})
    return Executor(gate_provider=(lambda: gate) if gate is not None else None,
                    edge_provider=(lambda: edge_report) if edge_report is not None else None,
                    capacity_provider=(lambda: capacity) if capacity is not None else None,
                    settings=settings, clock=clock or Clock())


# --------------------------------------------------------------------------- the master switch
def test_nothing_trades_until_it_is_turned_on():
    """The platform can be fully built, fully wired, and still not trading."""
    e = Executor(gate_provider=lambda: GATE, edge_provider=lambda: edge(), clock=Clock())
    v = e.may_open(S, 20.0)
    assert not v.allowed and v.max_stake == 0.0
    assert "execution is off" in v.reason


def test_an_allowed_strategy_gets_a_stake():
    v = ex(edge_report=edge()).may_open(S, 20.0)
    assert v.allowed and v.max_stake == 20.0 and v.gate_status == APPROVED_FOR_PAPER


def test_the_allow_list_stops_one_strategy_without_stopping_the_platform():
    e = ex(edge_report=edge(), strategies=frozenset({"SOMETHING_ELSE"}))
    v = e.may_open(S, 20.0)
    assert not v.allowed and "allow list" in v.reason
    assert e.may_open("SOMETHING_ELSE", 20.0).allowed is False, "still needs the gate"


# --------------------------------------------------------------------------- the gate
def test_the_gate_is_still_the_authority_on_whether_a_strategy_may_trade_at_all():
    for gate in ({}, {"approved_for_paper": [], "forward_test": []},
                 {"approved_for_paper": ["OTHER"]}, {"live_approved": [S]}):
        v = ex(gate=gate, edge_report=edge()).may_open(S, 20.0)
        assert not v.allowed and "validation gate" in v.reason, gate


def test_no_gate_at_all_means_nothing_trades():
    e = Executor(gate_provider=None, settings=ExecutionSettings(enabled=True), clock=Clock())
    assert e.may_open(S, 20.0).allowed is False


def test_a_gate_that_raises_is_not_permission():
    def boom():
        raise RuntimeError("the gate is down")
    e = Executor(gate_provider=boom, settings=ExecutionSettings(enabled=True), clock=Clock())
    v = e.may_open(S, 20.0)
    assert not v.allowed and "validation gate" in v.reason


def test_a_forward_test_strategy_trades_small():
    """Still gathering evidence; it should not be able to lose much while it gathers it."""
    v = ex(gate=FORWARD_GATE, edge_report=edge(), forward_test_stake=10.0).may_open(S, 500.0)
    assert v.allowed and v.max_stake == 10.0 and v.gate_status == FORWARD_TEST
    small = ex(gate=FORWARD_GATE, edge_report=edge(), forward_test_stake=10.0).may_open(S, 4.0)
    assert small.max_stake == 4.0, "the cap is a ceiling, not a floor"


# --------------------------------------------------------------------------- the decay check
def test_a_strategy_the_gate_approved_but_the_record_no_longer_does_is_stopped():
    """The whole reason this component exists. The gate is a promotion event; this is continuous."""
    e = ex(edge_report=edge(verdict="NEGATIVE_AFTER_COSTS", ci=(-0.04, -0.01)))
    v = e.may_open(S, 20.0)
    assert not v.allowed
    assert "losing money now" in v.reason
    assert "The gate approved it; the record no longer does." in v.reason


def test_a_negative_interval_stops_it_even_without_a_matching_verdict_string():
    v = ex(edge_report=edge(verdict="SOMETHING_NEW", ci=(-0.05, -0.02))).may_open(S, 20.0)
    assert not v.allowed and "below zero" in v.reason


def test_an_interval_straddling_zero_is_not_a_reason_to_refuse():
    v = ex(edge_report=edge(verdict="INCONCLUSIVE", ci=(-0.01, 0.02))).may_open(S, 20.0)
    assert v.allowed, "inconclusive is not the same as conclusively losing"


def test_too_few_scored_signals_cannot_veto():
    """Below the judging threshold there is nothing to judge. Refusing here would stop every new
    strategy from ever accumulating the record the gate needs."""
    v = ex(edge_report=edge(scored=4, ci=(-0.9, -0.5)), min_scored_to_judge=30).may_open(S, 20.0)
    assert v.allowed
    assert any("too few to judge" in c["detail"] for c in v.checks)


def test_the_decay_check_can_be_switched_off():
    v = ex(edge_report=edge(verdict="NEGATIVE_AFTER_COSTS", ci=(-0.04, -0.01)),
           stop_on_negative_edge=False).may_open(S, 20.0)
    assert v.allowed


def test_no_edge_report_is_not_a_reason_to_refuse_but_is_not_permission_either():
    """A missing report leaves the gate as the only authority, which is the pre-16B behaviour."""
    e = Executor(gate_provider=lambda: GATE, edge_provider=None,
                 settings=ExecutionSettings(enabled=True), clock=Clock())
    assert e.may_open(S, 20.0).allowed is True
    blocked = Executor(gate_provider=lambda: {}, edge_provider=None,
                       settings=ExecutionSettings(enabled=True), clock=Clock())
    assert blocked.may_open(S, 20.0).allowed is False


def test_an_edge_provider_that_raises_does_not_become_permission_to_ignore_the_gate():
    def boom():
        raise RuntimeError("down")
    e = Executor(gate_provider=lambda: {}, edge_provider=boom,
                 settings=ExecutionSettings(enabled=True), clock=Clock())
    assert e.may_open(S, 20.0).allowed is False


# --------------------------------------------------------------------------- version integrity
def test_a_strategy_spanning_two_versions_of_itself_is_refused():
    """Two versions pooled is not one strategy, and its interval is about nothing."""
    v = ex(edge_report=edge(versions=("v1", "v2"), mixed=True)).may_open(S, 20.0)
    assert not v.allowed
    assert "more than one version of itself" in v.reason


def test_the_version_check_can_be_switched_off():
    v = ex(edge_report=edge(versions=("v1", "v2"), mixed=True),
           stop_on_mixed_versions=False).may_open(S, 20.0)
    assert v.allowed


# --------------------------------------------------------------------------- capacity
def test_a_stake_above_the_measured_ceiling_is_cut_not_refused():
    """The trade is fine; the size is not. 14B measured where the edge stops surviving."""
    cap = {"strategies": {S: {"max_profitable_stake": 100.0}}}
    v = ex(edge_report=edge(), capacity=cap, capacity_fraction=0.5).may_open(S, 500.0)
    assert v.allowed and v.max_stake == 50.0
    assert any("cut to 50" in c["detail"] for c in v.checks)


def test_a_stake_inside_the_ceiling_is_left_alone():
    cap = {"strategies": {S: {"max_profitable_stake": 100.0}}}
    v = ex(edge_report=edge(), capacity=cap, capacity_fraction=0.5).may_open(S, 20.0)
    assert v.max_stake == 20.0


def test_an_unmeasured_ceiling_does_not_cap_anything():
    for cap in ({}, {"strategies": {}}, {"strategies": {S: {"max_profitable_stake": None}}},
                {"strategies": {S: {}}}):
        v = ex(edge_report=edge(), capacity=cap).may_open(S, 500.0)
        assert v.allowed and v.max_stake == 500.0, cap


def test_the_caps_compose_and_the_smallest_wins():
    cap = {"strategies": {S: {"max_profitable_stake": 100.0}}}
    v = ex(gate=FORWARD_GATE, edge_report=edge(), capacity=cap,
           forward_test_stake=10.0, capacity_fraction=0.5).may_open(S, 500.0)
    assert v.max_stake == 10.0, "forward-test cap is tighter than half the ceiling"


# --------------------------------------------------------------------------- housekeeping
def test_a_nonsense_stake_is_refused():
    for bad in (0, -5, None, "twenty", float("nan")):
        assert ex(edge_report=edge()).may_open(S, bad).allowed is False, bad


def test_it_never_raises_a_stake():
    cap = {"strategies": {S: {"max_profitable_stake": 10000.0}}}
    v = ex(edge_report=edge(), capacity=cap).may_open(S, 5.0)
    assert v.max_stake == 5.0


def test_the_report_says_why_nothing_is_trading():
    e = ex(edge_report=edge(verdict="NEGATIVE_AFTER_COSTS", ci=(-0.04, -0.01)))
    r = e.report()
    assert r["version"] == EXECUTOR_VERSION and r["enabled"] is True
    assert r["gate_allows"] == [S]
    assert r["strategies"][S]["allowed"] is False
    assert "losing money now" in r["strategies"][S]["reason"]
    assert r["edge_verdicts"] == {S: "NEGATIVE_AFTER_COSTS"}


def test_previewing_the_report_does_not_count_as_a_decision():
    e = ex(edge_report=edge())
    e.may_open(S, 20.0)
    e.report()
    e.report()
    assert sum(e.counts.values()) == 1, "report() previews; it must not pollute the decision counts"


def test_decisions_are_logged_for_the_owner():
    e = ex(edge_report=edge())
    for _ in range(3):
        e.may_open(S, 20.0)
    e.may_open("NOT_ALLOWED", 20.0)
    assert sum(e.counts.values()) == 4
    assert len(e.verdicts) == 4
    assert e.report()["recent"][-1]["strategy"] == "NOT_ALLOWED"


def test_the_decision_log_is_bounded():
    e = ex(edge_report=edge())
    for _ in range(400):
        e.may_open(S, 20.0)
    assert len(e.verdicts) == 200


# --------------------------------------------------------------------------- caching
def test_providers_are_not_called_once_per_order():
    calls = {"n": 0}

    def counting_gate():
        calls["n"] += 1
        return GATE
    e = Executor(gate_provider=counting_gate, edge_provider=lambda: edge(),
                 settings=ExecutionSettings(enabled=True, cache_seconds=300), clock=Clock())
    for _ in range(20):
        e.may_open(S, 20.0)
    assert calls["n"] == 1


def test_the_cache_expires_so_a_decayed_strategy_is_noticed():
    clock = Clock()
    state = {"report": edge()}
    e = Executor(gate_provider=lambda: GATE, edge_provider=lambda: state["report"],
                 settings=ExecutionSettings(enabled=True, cache_seconds=300), clock=clock)
    assert e.may_open(S, 20.0).allowed
    state["report"] = edge(verdict="NEGATIVE_AFTER_COSTS", ci=(-0.04, -0.01))
    assert e.may_open(S, 20.0).allowed, "still inside the cache window"
    clock.advance(seconds=301)
    assert e.may_open(S, 20.0).allowed is False, "the window expired and the decay is seen"
