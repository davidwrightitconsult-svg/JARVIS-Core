"""
FRIDAY Quant Core — strategy scorecard (Phase 7C).

Per strategy, over a window: how many signals were scored, how often the
price moved the predicted way, what they made AFTER crossing the spread,
with a 95% confidence interval, split by entry price band (cheap long shots
behave differently: a 1c spread is 25% of a 4c price) and by direction,
plus results held to resolution where markets have resolved.

Verdicts are deliberately conservative:
  INSUFFICIENT_DATA     fewer than MIN_SAMPLE scored signals (says how many more)
  NEGATIVE_AFTER_COSTS  95% CI of mean net entirely below zero
  INCONCLUSIVE          CI straddles zero
  PROMISING             CI entirely above zero — still NOT a validated edge:
                        Phases 8-9 test it out of sample before anything
                        may depend on it

8C: held-to-resolution results carry their own confidence interval and
verdict. A strategy judged on resolution (CALIBRATION_EDGE — the fair-value
model's claim is about the final outcome, not the next hours' price) takes
its headline verdict from those, over a 365-day window (its markets can
take weeks to resolve); the others keep the horizon verdict.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from .scoring import cost_of
from .strategies import LIBRARY

SCORECARD_VERSION = "8C.1"
MIN_SAMPLE = 30
BANDS = (("<0.10", 0.0, 0.10), ("0.10-0.90", 0.10, 0.90), (">0.90", 0.90, 1.01))


def _band(mid: float | None) -> str:
    for name, lo, hi in BANDS:
        if mid is not None and lo <= mid < hi:
            return name
    return "unknown"


def _interval(values: list[float]) -> tuple[float | None, list[float] | None]:
    n = len(values)
    mean = statistics.fmean(values) if values else None
    sd = statistics.stdev(values) if n >= 2 else None
    half = 1.96 * sd / math.sqrt(n) if sd is not None else None
    return mean, ([round(mean - half, 4), round(mean + half, 4)] if half is not None else None)


def _resolution_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if r["resolution_net"] is not None]
    nets = [r["resolution_net"] for r in done]
    rocs = [r["resolution_net"] / c for r in done if (c := cost_of(r))]
    mean, ci = _interval(nets)
    return {"resolved": len(nets), "scored": len(nets),
            "avg_net": round(mean, 4) if mean is not None else None, "ci95_net": ci,
            "win_rate": round(sum(x > 0 for x in nets) / len(nets), 3) if nets else None,
            "avg_return_on_cost": round(statistics.fmean(rocs), 4) if rocs else None}


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [r["net"] for r in rows if r["net"] is not None]
    moves = [r["mid_move"] for r in rows if r["mid_move"] is not None]
    rocs = [r["return_on_cost"] for r in rows if r["return_on_cost"] is not None]
    n = len(nets)
    mean = statistics.fmean(nets) if nets else None
    sd = statistics.stdev(nets) if n >= 2 else None
    half = 1.96 * sd / math.sqrt(n) if sd is not None else None
    return {
        "scored": n,
        "hit_rate": round(sum(m > 0 for m in moves) / len(moves), 3) if moves else None,
        "flat_rate": round(sum(m == 0 for m in moves) / len(moves), 3) if moves else None,
        "win_rate_net": round(sum(x > 0 for x in nets) / n, 3) if n else None,
        "avg_mid_move": round(statistics.fmean(moves), 4) if moves else None,
        "avg_net": round(mean, 4) if mean is not None else None,
        "ci95_net": [round(mean - half, 4), round(mean + half, 4)] if half is not None else None,
        "avg_return_on_cost": round(statistics.fmean(rocs), 4) if rocs else None,
        "t_stat": round(mean / (sd / math.sqrt(n)), 2) if sd else None,
    }


def verdict(stats: dict[str, Any], min_sample: int = MIN_SAMPLE) -> str:
    n = stats["scored"]
    if n < min_sample:
        return f"INSUFFICIENT_DATA ({min_sample - n} more scored signals needed)"
    lo, hi = stats["ci95_net"] or (None, None)
    if hi is not None and hi < 0:
        return "NEGATIVE_AFTER_COSTS"
    if lo is not None and lo > 0:
        return "PROMISING (not validated — Phases 8-9)"
    return "INCONCLUSIVE"


def build_scorecard(store, days: float = 30, min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    rows = store.outcomes(days=days)
    long_rows = store.outcomes(days=max(days, 365)) if any(s.measure == "resolution" for s in LIBRARY) else rows
    out = []
    for s in LIBRARY:
        mine = [r for r in (long_rows if s.measure == "resolution" else rows) if r["strategy"] == s.name]
        scored = [r for r in mine if r["status"] in ("SCORED", "RESOLVED_BEFORE_EXIT")]
        stats = _stats(scored)
        held = _resolution_stats(mine)
        held["verdict"] = verdict(held, min_sample)
        out.append({
            "strategy": s.name, "horizon_hours": s.horizon_hours, "hypothesis": s.hypothesis,
            "signals": len(mine), "open": sum(1 for r in mine if r["status"] is None),
            "no_exit_data": sum(1 for r in mine if r["status"] == "NO_EXIT_DATA"),
            **stats, "judged_on": s.measure,
            "verdict": held["verdict"] if s.measure == "resolution" else verdict(stats, min_sample),
            "held_to_resolution": held,
            "by_band": {name: _stats([r for r in scored if _band(r["mid"]) == name])
                        for name, _, _ in BANDS},
            "by_direction": {d: _stats([r for r in scored if r["direction"] == d]) for d in ("UP", "DOWN")},
        })
    calibration = None
    try:
        from .calibration import build_calibration
        cal = build_calibration(store)
        calibration = cal if cal["samples"] else None
    except Exception:
        calibration = None
    return {
        "scorecard_version": SCORECARD_VERSION, "window_days": days, "min_sample": min_sample,
        "strategies": out, "calibration": calibration,
        "note": ("Net results cross the spread at entry and exit. PROMISING is not a validated "
                 "edge; nothing here authorises any trade."),
    }


def render_markdown(card: dict[str, Any], day: str) -> str:
    lines = [f"# Strategy scorecard — {day}", "",
             "> [!note] Measured, not validated. Net = after crossing the spread at entry and exit.",
             "> A PROMISING verdict still needs out-of-sample validation (Phases 8-9). Execution is locked.",
             "", f"Window: last {card['window_days']:g} days · minimum sample {card['min_sample']}", "",
             "| Strategy | Horizon | Signals | Scored | Hit rate | Avg net (¢) | 95% CI (¢) | Avg return on cost | Verdict |",
             "|---|---|---|---|---|---|---|---|---|"]

    def c(x):
        return "—" if x is None else f"{x * 100:+.2f}"

    def p(x):
        return "—" if x is None else f"{x * 100:.0f}%"

    for s in card["strategies"]:
        ci = s["ci95_net"]
        lines.append(f"| {s['strategy']} | {s['horizon_hours']:g}h | {s['signals']} | {s['scored']} | "
                     f"{p(s['hit_rate'])} | {c(s['avg_net'])} | "
                     f"{'—' if not ci else c(ci[0]) + ' … ' + c(ci[1])} | {p(s['avg_return_on_cost'])} | "
                     f"{s['verdict']} |")
    lines += ["", "## By entry price band (avg net ¢ · scored)", "",
              "| Strategy | <0.10 | 0.10-0.90 | >0.90 |", "|---|---|---|---|"]
    for s in card["strategies"]:
        cells = [f"{c(b['avg_net'])} · {b['scored']}" for b in s["by_band"].values()]
        lines.append(f"| {s['strategy']} | " + " | ".join(cells) + " |")
    lines += ["", "## Held to resolution", "",
              "| Strategy | Resolved | Avg net (¢) | 95% CI (¢) | Win rate | Verdict |", "|---|---|---|---|---|---|"]
    for s in card["strategies"]:
        h = s["held_to_resolution"]
        ci = h["ci95_net"]
        lines.append(f"| {s['strategy']}{' (judged here)' if s.get('judged_on') == 'resolution' else ''} | "
                     f"{h['resolved']} | {c(h['avg_net'])} | {'—' if not ci else c(ci[0]) + ' … ' + c(ci[1])} | "
                     f"{p(h['win_rate'])} | {h['verdict']} |")
    if card.get("calibration"):
        from .calibration import render_calibration_markdown
        lines += ["", render_calibration_markdown(card["calibration"]).rstrip()]
    lines += ["", "## Hypotheses", ""] + [f"- **{s['strategy']}** — {s['hypothesis']}" for s in card["strategies"]]
    return "\n".join(lines) + "\n"
