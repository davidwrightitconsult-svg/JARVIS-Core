"""
FRIDAY risk drills (Phase 10C): try to break the Risk Governor and the paper
ledger before any strategy is allowed near them.

Every drill runs on a TEMPORARY risk store with paper money and a made-up
gate. Nothing here reads the real store, the network or Claude, and nothing
can place a trade. Each drill returns what it did, what it measured and
whether every invariant held.

    python -m friday.risk drill
"""

from __future__ import annotations

import math
import random
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .governor import APPROVED, REDUCED, REJECTED, Portfolio, Proposal, RiskGovernor, RiskSettings
from .ledger import LedgerError, PaperLedger
from .state import RiskState

DRILLS_VERSION = "10C.1"
ALLOWED = ("ALPHA", "BETA", "GAMMA", "DELTA")
GATE = {"approved_for_paper": ["ALPHA", "BETA"], "forward_test": ["GAMMA", "DELTA"], "live_approved": []}
CENT = 1e-6                                    # stakes are whole cents, rounded down


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def _rig(folder: str, name: str, settings: RiskSettings, gate=lambda: GATE):
    path = Path(folder) / f"{name}.db"
    clock = _Clock()
    state = RiskState(path)
    return RiskGovernor(settings, state, gate, clock), PaperLedger(path, settings.bankroll, clock), state, clock, path


def _within_caps(s: RiskSettings, before: Portfolio, d: dict[str, Any]) -> list[str]:
    """The book AFTER this approval must sit inside every cap, measured on the equity it was sized on."""
    stake, eq, bad = d["approved_stake"], before.equity, []
    in_market = sum(p.stake for p in before.positions if p.market_id == d["market_id"]) + stake
    in_strategy = sum(p.stake for p in before.positions if p.strategy == d["strategy"]) + stake
    if stake > s.max_position_fraction * eq + CENT:
        bad.append("position cap")
    if in_market > s.max_market_fraction * eq + CENT:
        bad.append("market cap")
    if in_strategy > s.max_strategy_fraction * eq + CENT:
        bad.append("strategy cap")
    if before.exposure + stake > s.max_total_exposure * eq + CENT:
        bad.append("total exposure cap")
    if len({p.market_id for p in before.positions} | {d["market_id"]}) > s.max_open_positions:
        bad.append("open positions")
    if d["requested_stake"] is None or stake > d["requested_stake"] + 1e-9:
        bad.append("approved more than asked")
    if stake < s.min_stake - 1e-9:
        bad.append("below the minimum stake")
    return bad


def storm(folder: str, settings: RiskSettings, steps: int = 3000, seed: int = 10) -> dict[str, Any]:
    """Thousands of random proposals, price moves and closes over many days, including garbage."""
    rng = random.Random(seed)
    g, ledger, state, clock, _ = _rig(folder, "storm", settings)
    prices: dict[int, float] = {}
    broken: list[str] = []
    counts = {APPROVED: 0, REDUCED: 0, REJECTED: 0}
    opened = 0
    junk = [float("nan"), float("inf"), -50.0, 0.0, 1e12]
    for i in range(steps):
        clock.advance(minutes=rng.choice((5, 20, 90, 400)))
        for pid in list(prices):
            prices[pid] = min(0.99, max(0.0, prices[pid] + rng.gauss(0, 0.04)))
        roll = rng.random()
        if roll < 0.25 and prices:
            pid = rng.choice(list(prices))
            ledger.close(pid, prices.pop(pid), "storm")
            continue
        before = ledger.portfolio(prices)
        halted, killed = state.drawdown_halt()["engaged"], state.kill_switch()["engaged"]
        strategy = rng.choice(ALLOWED + ("UNTESTED", "", "ALPH"))
        mode = "LIVE" if rng.random() < 0.05 else "PAPER"
        stake = rng.choice(junk) if rng.random() < 0.08 else rng.choice((1, 5, 20, 60, 400, 5000)) * rng.random() * 2
        d = g.evaluate(Proposal(strategy, f"mkt{rng.randrange(40)}", stake, mode=mode), before)
        counts[d["verdict"]] += 1
        if d["verdict"] == REJECTED:
            if d["approved_stake"] != 0:
                broken.append(f"step {i}: rejected with a stake")
            try:
                ledger.open(d, 0.5)
                broken.append(f"step {i}: a rejected decision opened a position")
            except LedgerError:
                pass
            if rng.random() < 0.02 and (halted or state.drawdown_halt()["engaged"]):
                state.resume("RESUME", "drill")                         # the owner looks, then resumes
            continue
        why = _within_caps(settings, before, d)
        if strategy not in ALLOWED or mode != "PAPER" or halted or killed:
            why.append("approved what must be refused")
        if before.equity <= before.day_start_equity * (1 - settings.daily_loss_limit):
            why.append("approved past the daily loss limit")
        if before.equity <= before.peak_equity * (1 - settings.max_drawdown):
            why.append("approved past the drawdown halt")
        if why:
            broken.append(f"step {i}: {', '.join(why)}")
        price = rng.uniform(0.05, 0.95)
        try:
            pos = ledger.open(d, price)
        except LedgerError as exc:
            broken.append(f"step {i}: approved but the ledger refused ({exc})")
            continue
        prices[pos["position_id"]] = price
        opened += 1
    summary = ledger.summary(prices)
    if summary["cash"] < -1e-6 or not math.isfinite(summary["equity"]):
        broken.append("cash below zero or equity not finite")
    return {"name": "random storm", "passed": not broken, "broken": broken[:5],
            "detail": f"{steps} steps over {(clock.now - datetime(2026, 1, 5, 12, tzinfo=timezone.utc)).days} days: "
                      f"{counts[APPROVED]} approved, {counts[REDUCED]} reduced, {counts[REJECTED]} rejected, "
                      f"{opened} positions opened; ended at ${summary['equity']:.2f}; no limit was ever exceeded"}


def wipeout(folder: str, settings: RiskSettings) -> dict[str, Any]:
    """Fill the book to the limit, then every open position goes to zero at the same moment."""
    g, ledger, state, clock, path = _rig(folder, "wipeout", settings)
    prices, i = {}, 0
    while True:
        d, pos = ledger.open_checked(g, Proposal(ALLOWED[i % 4], f"m{i}", 10_000), 0.5, prices)
        i += 1
        if pos is None:
            if i > 400 or all(g.evaluate(Proposal(s, f"z{i}", 10_000), ledger.portfolio(prices), record=False)["verdict"]
                              == REJECTED for s in ALLOWED):
                break
            continue
        prices[pos["position_id"]] = 0.5
    full = ledger.portfolio(prices)
    exposure = full.exposure / full.equity
    after = ledger.portfolio({pid: 0.0 for pid in prices})
    loss = 1 - after.equity / full.equity
    d1 = g.evaluate(Proposal("ALPHA", "new", 10), after)
    restarted = RiskGovernor(settings, RiskState(path), lambda: GATE, clock)
    for pid in prices:
        ledger.close(pid, 0.0, "wipeout")
    clock.advance(days=3)                                                # days later, equity is its own new day start
    d2 = restarted.evaluate(Proposal("ALPHA", "new", 10), ledger.portfolio())
    try:
        state.resume("resume please", "drill"); wrong = False
    except PermissionError:
        wrong = True
    state.resume("RESUME", "drill")
    d3 = g.evaluate(Proposal("ALPHA", "new", 10), ledger.portfolio())
    ok = (exposure <= settings.max_total_exposure + 1e-6 and loss <= settings.max_total_exposure + 1e-6
          and d1["reason"] == "DRAWDOWN_HALT" and d2["reason"] == "DRAWDOWN_HALT" and wrong
          and d3["verdict"] in (APPROVED, REDUCED) and len(prices) <= settings.max_open_positions)
    return {"name": "everything goes to zero at once", "passed": ok, "worst_loss": round(loss, 4), "broken": [],
            "detail": f"{len(prices)} positions, {exposure:.1%} of equity at risk; all to zero cost {loss:.1%} "
                      f"(the bound is the {settings.max_total_exposure:.0%} exposure cap); halted, still halted "
                      f"after a restart and 3 days, released only by RESUME"}


def bleed(folder: str, settings: RiskSettings) -> dict[str, Any]:
    """Every trade is a total loss, day after day. How far can the account fall before it is stopped?"""
    g, ledger, state, clock, _ = _rig(folder, "bleed", settings)
    days, trades, worst_day, n = 0, 0, 0.0, 0
    while days < 60 and not state.drawdown_halt()["engaged"]:
        days += 1
        start = ledger.portfolio().equity
        while True:
            n += 1
            d, pos = ledger.open_checked(g, Proposal(ALLOWED[n % 4], f"b{n}", 10_000), 0.5)
            if pos is None:
                break
            ledger.close(pos["position_id"], 0.0, "bleed"); trades += 1
        worst_day = max(worst_day, 1 - ledger.portfolio().equity / start)
        clock.advance(days=1)
    s = ledger.summary()
    day_bound = settings.daily_loss_limit + settings.max_position_fraction
    dd_bound = settings.max_drawdown + day_bound
    ok = (state.drawdown_halt()["engaged"] and worst_day <= day_bound + 1e-6 and s["drawdown"] <= dd_bound + 1e-6)
    return {"name": "every trade loses everything", "passed": ok, "worst_day": round(worst_day, 4),
            "final_drawdown": s["drawdown"], "broken": [],
            "detail": f"{trades} straight total losses over {days} days: worst day {worst_day:.1%} "
                      f"(bound {day_bound:.0%}), stopped by the drawdown halt at {s['drawdown']:.1%} below the peak "
                      f"(bound {dd_bound:.0%}), ${s['equity']:.2f} left of ${settings.bankroll:g}"}


def rush(folder: str, settings: RiskSettings, threads: int = 40) -> dict[str, Any]:
    """Many proposals arrive at the same instant, for one market and for many."""
    g, ledger, _, _, _ = _rig(folder, "rush", settings)
    errors: list[str] = []

    def go(market, strategy):
        try:
            ledger.open_checked(g, Proposal(strategy, market, 10_000), 0.5)
        except Exception as exc:                                   # noqa: BLE001 - a drill reports everything
            errors.append(f"{type(exc).__name__}: {exc}")
    workers = [threading.Thread(target=go, args=("same", ALLOWED[i % 4])) for i in range(threads)]
    workers += [threading.Thread(target=go, args=(f"r{i}", ALLOWED[i % 4])) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    pf, s = ledger.portfolio(), settings
    same = sum(p.stake for p in pf.positions if p.market_id == "same")
    by_strategy = max(sum(p.stake for p in pf.positions if p.strategy == a) for a in ALLOWED)
    markets = len({p.market_id for p in pf.positions})
    ok = (not errors and same <= s.max_market_fraction * s.bankroll + CENT
          and pf.exposure <= s.max_total_exposure * s.bankroll + CENT
          and by_strategy <= s.max_strategy_fraction * s.bankroll + CENT and markets <= s.max_open_positions
          and ledger.summary()["cash"] >= 0)
    return {"name": "a rush of simultaneous proposals", "passed": ok, "broken": errors[:3],
            "detail": f"{len(workers)} at once: ${same:.2f} in the contested market (cap "
                      f"${s.max_market_fraction * s.bankroll:.2f}), ${pf.exposure:.2f} at risk in {markets} markets "
                      f"(caps ${s.max_total_exposure * s.bankroll:.2f}, {s.max_open_positions})"}


def bad_gate(folder: str, settings: RiskSettings) -> dict[str, Any]:
    """The validation gate breaks, lies about its shape, or 'approves' live trading."""
    def boom():
        raise RuntimeError("market store offline")
    cases = {"raises": boom, "returns nothing": lambda: None, "empty": lambda: {}, "a string": lambda: "ALPHA",
             "a list": lambda: ["ALPHA"], "names as one string": lambda: {"approved_for_paper": "ALPHA BETA"},
             "only live approved": lambda: {"live_approved": ["ALPHA"]}, "no provider": None}
    wrong = []
    for label, provider in cases.items():
        g = RiskGovernor(settings, RiskState(Path(folder) / "gate.db"), provider)
        for strategy in ("ALPHA", "ALPH", "A"):
            if g.evaluate(Proposal(strategy, "m", 10))["verdict"] != REJECTED:
                wrong.append(f"{label}: {strategy} was approved")
    g = RiskGovernor(settings, RiskState(Path(folder) / "gate.db"),
                     lambda: {**GATE, "live_approved": ["ALPHA"]})
    if g.evaluate(Proposal("ALPHA", "m", 10, mode="LIVE"))["reason"] != "LIVE_TRADING_LOCKED":
        wrong.append("a gate claiming live approval unlocked LIVE")
    return {"name": "a broken or lying validation gate", "passed": not wrong, "broken": wrong[:5],
            "detail": f"{len(cases)} kinds of broken gate: every proposal refused; LIVE stays locked even when the "
                      f"gate itself claims live approval"}


def kill_and_restart(folder: str, settings: RiskSettings) -> dict[str, Any]:
    """The kill switch, across a restart, with wrong release words."""
    g, ledger, state, clock, path = _rig(folder, "kill", settings)
    d, pos = ledger.open_checked(g, Proposal("ALPHA", "held", 20), 0.5)
    state.engage_kill_switch("drill", "drill")
    g2 = RiskGovernor(settings, RiskState(path), lambda: GATE, clock)
    refused = all(g2.evaluate(Proposal(s, f"k{i}", 5))["reason"] == "KILL_SWITCH" for i, s in enumerate(ALLOWED))
    wrong_words = 0
    for word in ("", "resume", "RESUME ", "yes", "RESUME;", "Resume"):
        try:
            RiskState(path).resume(word, "drill")
        except PermissionError:
            wrong_words += 1
    still = g2.evaluate(Proposal("ALPHA", "k", 5))["reason"] == "KILL_SWITCH"
    closed = ledger.close(pos["position_id"], 0.6, "exit while killed")["status"] == "CLOSED"
    RiskState(path).resume("RESUME", "drill")
    back = g2.evaluate(Proposal("ALPHA", "k", 5))["verdict"] == APPROVED
    ok = refused and wrong_words == 6 and still and closed and back
    return {"name": "kill switch across a restart", "passed": ok, "broken": [],
            "detail": "engaged, restarted: every proposal refused; six wrong release words refused; an existing "
                      "position could still be closed; only RESUME released it"}


def unsafe_settings(folder: str, settings: RiskSettings) -> dict[str, Any]:
    """Settings that would loosen the limits past the hard ceilings must not load."""
    tries = [dict(max_position_fraction=0.11, max_market_fraction=0.2), dict(max_total_exposure=0.61),
             dict(max_drawdown=0.26), dict(daily_loss_limit=0.11), dict(max_position_fraction=0.05),
             dict(max_strategy_fraction=0.5), dict(bankroll=0), dict(bankroll=-100), dict(min_stake=0),
             dict(max_open_positions=0), dict(max_drawdown=0), dict(max_total_exposure=float("nan"))]
    loaded = []
    for kw in tries:
        try:
            RiskSettings(**kw); loaded.append(str(kw))
        except (ValueError, TypeError):
            pass
    return {"name": "settings beyond the hard ceilings", "passed": not loaded, "broken": loaded[:5],
            "detail": f"{len(tries)} unsafe settings tried (position over 10%, exposure over 60%, drawdown over 25%, "
                      f"daily loss over 10%, limits that do not nest, zero or negative money): none loaded"}


DRILLS = (storm, wipeout, bleed, rush, bad_gate, kill_and_restart, unsafe_settings)


def run_drills(settings: RiskSettings | None = None) -> dict[str, Any]:
    settings = settings or RiskSettings()
    results = []
    with tempfile.TemporaryDirectory(prefix="friday-drill-") as folder:
        for drill in DRILLS:
            try:
                results.append(drill(folder, settings))
            except Exception as exc:                               # a drill that crashes has failed
                results.append({"name": drill.__name__, "passed": False, "broken": [],
                                "detail": f"the drill crashed: {type(exc).__name__}: {str(exc)[:200]}"})
    worst = next((r for r in results if "worst_loss" in r), {})
    slow = next((r for r in results if "final_drawdown" in r), {})
    return {"drills_version": DRILLS_VERSION, "settings": settings.as_dict(), "results": results,
            "passed": all(r["passed"] for r in results),
            "bounds": {"worst_instant_loss": worst.get("worst_loss"), "worst_day_without_open_positions": slow.get("worst_day"),
                       "slow_bleed_stopped_at": slow.get("final_drawdown")},
            "store": "temporary", "execution_locked": True}


def render_drills(report: dict[str, Any]) -> str:
    lines = [f"FRIDAY risk drills {report['drills_version']} (temporary store, paper money)"]
    for r in report["results"]:
        lines.append(f"{'PASS' if r['passed'] else 'FAIL'}: {r['name']}")
        lines.append(f"      {r['detail']}")
        for b in r.get("broken") or []:
            lines.append(f"      BROKEN: {b}")
    lines.append("RISK DRILLS: " + ("PASS" if report["passed"] else "FAIL"))
    return "\n".join(lines)
