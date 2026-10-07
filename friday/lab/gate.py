"""
FRIDAY Validation Lab — the validation gate (Phase 9C).

One answer, in one place, to "what is FRIDAY allowed to act on?". The Risk
Governor (Phase 10), Decision Engine (Phase 11) and Paper Trading
(Phase 12) read the gate; nothing else decides it.

Each strategy gets exactly one status:

  APPROVED_FOR_PAPER  the evidence FRIDAY has passes its own test. It may be
                      traded with PAPER money only.
  FORWARD_TEST        it cannot be tested on history without knowing the
                      answers (research forecasts made by Claude today
                      about markets that have already resolved would leak
                      the outcome). It may be paper-traded as a hypothesis
                      under test; its paper record is the evidence.
  NOT_APPROVED        tested and not supported, or not enough evidence. It
                      may be recorded and scored, never sized.

Evidence used:
  CALIBRATION_EDGE     the 9B robustness report (end-anchored, out of sample):
                       approved only if `robust`
  book strategies      the 11C test of each registered rule on hourly history:
                       approved only if it passed development (99.5%) AND the
                       untouched holdout (95%), with the settings the registry
                       holds today
  feature strategies   FRIDAY's live scorecard (7C): each signal scored after
                       crossing the spread. Approved only if the 95% interval
                       of the net result is entirely above zero on at least
                       MIN_SCORED signals over at least MIN_DAYS days
  PRICING_INCONSISTENCY the 9C scan of recorded order books: approved only
                       if PRESENT
  RESEARCH_FORECAST    FORWARD_TEST
  declared forward     FORWARD_TEST while its own paper record is being
  tests (12B)          gathered under a rule fixed in advance; NOT_APPROVED
                       once that rule says it failed or its loss limit is used;
                       APPROVED_FOR_PAPER only after it succeeded AND the owner
                       promoted it (12C), and only while its record holds up

`live_approved` is always empty: the gate cannot approve live trading.
That is Phase 17's audit and the owner's decision; execution stays locked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from friday.quant.scorecard import build_scorecard
from friday.quant.strategies import EDGE_STRATEGY

from .backtest import BacktestSettings
from .inconsistency import scan_inconsistencies
from .robustness import run_robustness

GATE_VERSION = "12C.1"
MIN_SCORED = 100
MIN_DAYS = 7
APPROVED, FORWARD, NOT_APPROVED = "APPROVED_FOR_PAPER", "FORWARD_TEST", "NOT_APPROVED"


def _feature_strategy(s: dict[str, Any], days_active: int) -> dict[str, Any]:
    ci = s["ci95_net"]
    enough = s["scored"] >= MIN_SCORED and days_active >= MIN_DAYS
    if enough and ci and ci[0] > 0:
        status, reason = APPROVED, "net result after the spread is above zero with 95% confidence"
    elif enough and ci and ci[1] < 0:
        status, reason = NOT_APPROVED, "loses money after the spread (95% interval entirely below zero)"
    elif enough:
        status, reason = NOT_APPROVED, "no measurable result either way after the spread"
    else:
        status, reason = NOT_APPROVED, (f"not enough evidence: {s['scored']} scored signals over {days_active} days "
                                        f"(needs {MIN_SCORED} over {MIN_DAYS})")
    return {"strategy": s["strategy"], "status": status, "reason": reason, "evidence": {
        "source": "live scorecard (signals scored after crossing the spread)", "signals": s["signals"],
        "scored": s["scored"], "days": days_active, "hit_rate": s["hit_rate"], "avg_net": s["avg_net"],
        "ci95_net": ci, "held_to_resolution": {k: s["held_to_resolution"].get(k) for k in ("resolved", "avg_net")}}}


def build_gate(store, settings: BacktestSettings | None = None, excluded: frozenset[str] | None = None,
               scorecard_days: float = 60, robustness: dict[str, Any] | None = None,
               forward_tests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card = build_scorecard(store, days=scorecard_days)
    days_by: dict[str, set[str]] = {}
    for r in store.outcomes(days=scorecard_days):
        if r["status"] in ("SCORED", "RESOLVED_BEFORE_EXIT"):
            days_by.setdefault(r["strategy"], set()).add(str(r["observed_at"])[:10])
    rob = robustness or run_robustness(store, settings, excluded)
    passes = [h for h, v in rob["by_horizon"].items() if v["verdict"].startswith("PASSES")]
    strategies = [{
        "strategy": EDGE_STRATEGY, "status": APPROVED if rob["robust"] else NOT_APPROVED,
        "reason": rob["conclusion"],
        "evidence": {"source": "walk-forward backtest on end-anchored prices (9B)",
                     "markets_24h": rob["by_horizon"]["24h"]["markets"], "bets_24h": rob["by_horizon"]["24h"]["bets"],
                     "horizons_passing": passes}}]
    for s in card["strategies"]:
        if s["strategy"] != EDGE_STRATEGY:
            strategies.append(_feature_strategy(s, len(days_by.get(s["strategy"], ()))))
    inc = scan_inconsistencies(store, excluded=excluded)
    strategies.append({
        "strategy": "PRICING_INCONSISTENCY", "status": APPROVED if inc["verdict"].startswith("PRESENT") else NOT_APPROVED,
        "reason": inc["verdict"],
        "evidence": {"source": "FRIDAY's recorded order books (9C)", "observations": inc["observations"],
                     "episodes_1c": inc["by_threshold"]["1c"]["episodes"], "days_1c": inc["by_threshold"]["1c"]["days"],
                     "max_locked": inc["by_threshold"]["0.5c"]["max_locked"]}})
    strategies.append({
        "strategy": "RESEARCH_FORECAST", "status": FORWARD,
        "reason": ("cannot be tested on history without knowing the outcomes; its paper-trading record "
                   "(Phase 12) is the evidence"),
        "evidence": {"source": "none yet", "paper_trades": 0}})
    from .bookrun import gate_entries            # 11C: the book rules, once tested on hourly history
    strategies.extend(gate_entries(store))
    for entry in forward_tests or ():            # 12B: declared forward tests, judged on their own paper record
        if not entry.get("strategy"):
            continue
        if entry.get("status") in (FORWARD, NOT_APPROVED):
            strategies.append(entry)
        elif entry.get("status") == APPROVED and entry.get("evidence", {}).get("promoted_by_owner") is True:
            strategies.append(entry)                 # a forward test cannot approve itself: only the owner's recorded promotion does (12C)
    by_status = {k: [s["strategy"] for s in strategies if s["status"] == k] for k in (APPROVED, FORWARD, NOT_APPROVED)}
    if by_status[APPROVED]:
        summary = "Approved for PAPER trading: " + ", ".join(by_status[APPROVED]) + "."
    else:
        summary = ("No strategy has evidence of an edge. Nothing is approved; research forecasts may be "
                   "paper-traded as a hypothesis under test.")
    return {"gate_version": GATE_VERSION, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategies": strategies, "approved_for_paper": by_status[APPROVED], "forward_test": by_status[FORWARD],
            "not_approved": by_status[NOT_APPROVED], "live_approved": [], "execution_locked": True,
            "summary": summary,
            "note": ("The gate can approve PAPER trading only. Live trading needs the Phase 17 audit and the "
                     "owner's decision; execution is locked.")}


def allowed(gate: dict[str, Any], strategy: str) -> bool:
    """True if `strategy` may be PAPER-traded (approved, or a forward test). Never implies live."""
    return strategy in gate["approved_for_paper"] or strategy in gate["forward_test"]


def render_gate(g: dict[str, Any]) -> str:
    lines = [f"VALIDATION GATE {g['gate_version']} — {g['at']}", ""]
    for s in g["strategies"]:
        lines.append(f"  {s['status']:<19} {s['strategy']:<22} {s['reason']}")
        e = s["evidence"]
        if e.get("book") and e["trades"]:
            iv = e["interval"]
            rng = "—" if not iv else f"{iv[0] * 100:+.2f} … {iv[1] * 100:+.2f}c"
            lines.append(f"  {'':<19} {'':<22} {e['trades']} trades on {e['days']} days in {e['markets']} markets · "
                         f"net {e['avg_net'] * 100:+.2f}c a share after {e['cost_per_side'] * 100:g}c a side (99.5% {rng}) · "
                         f"before costs {e['avg_gross'] * 100:+.2f}c")
        if "scored" in e:
            ci = e["ci95_net"]
            net = "—" if e["avg_net"] is None else f"{e['avg_net'] * 100:+.2f}c"
            rng = "—" if not ci else f"{ci[0] * 100:+.2f} … {ci[1] * 100:+.2f}c"
            lines.append(f"  {'':<19} {'':<22} {e['signals']} signals, {e['scored']} scored over {e['days']} days · "
                         f"hit rate {e['hit_rate']} · net {net} (95% {rng}) · held to resolution: "
                         f"{e['held_to_resolution']['resolved']} resolved, net {e['held_to_resolution']['avg_net']}")
    lines += ["", f"approved for paper: {g['approved_for_paper'] or 'none'} · forward test: {g['forward_test']} · "
                  f"live approved: {g['live_approved'] or 'none'}", "SUMMARY: " + g["summary"], g["note"]]
    return "\n".join(lines)
