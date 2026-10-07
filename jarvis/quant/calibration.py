"""
FRIDAY Quant Core — market calibration (Phase 8A).

"When Polymarket priced an outcome at p, how often did it happen?"
Computed from the backfilled dataset of resolved markets (8A backfill):

  per price bin   samples, markets, mean price, actual win rate with a
                  Wilson 95% interval, bias = win rate - mean price, and a
                  verdict: OVERPRICED (interval entirely below the mean
                  price), UNDERPRICED (entirely above), CONSISTENT, or
                  FEW_SAMPLES (< 30)
  overall         Brier score (lower is better; 0.25 = coin flip),
                  log loss, and the same for a no-skill baseline

Every outcome token of a market is one sample (a binary market contributes
p and 1-p), so the table is symmetric by construction; `markets` counts
distinct markets so a thin bin is visible as thin.

An OVERPRICED low bin is the favourite-longshot bias measured directly. It
is a property of past prices, not yet an executable edge: 8B prices it
against live asks and costs.
"""

from __future__ import annotations

import math
from typing import Any

CALIBRATION_VERSION = "8B.2"
BIN_EDGES = (0.0, 0.02, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98, 1.0)
MIN_BIN_SAMPLE = 30
DEFAULT_HORIZON = 24


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _bin(price: float) -> int:
    for i in range(len(BIN_EDGES) - 1):
        if price < BIN_EDGES[i + 1] or i == len(BIN_EDGES) - 2:
            return i
    return len(BIN_EDGES) - 2


def calibration_table(points: list[dict[str, Any]], min_bin: int = MIN_BIN_SAMPLE) -> dict[str, Any]:
    bins: list[dict[str, Any]] = []
    grouped: dict[int, list[dict[str, Any]]] = {}
    for p in points:
        grouped.setdefault(_bin(p["price"]), []).append(p)
    for i in range(len(BIN_EDGES) - 1):
        rows = grouped.get(i, [])
        n = len(rows)
        wins = sum(1 for r in rows if r["payoff"] >= 0.5)
        mean_price = sum(r["price"] for r in rows) / n if n else None
        rate = wins / n if n else None
        ci = wilson(wins, n)
        if n < min_bin:
            verdict = "FEW_SAMPLES"
        elif ci[1] < mean_price:
            verdict = "OVERPRICED"
        elif ci[0] > mean_price:
            verdict = "UNDERPRICED"
        else:
            verdict = "CONSISTENT"
        bins.append({
            "bin": f"{BIN_EDGES[i]:.2f}-{BIN_EDGES[i + 1]:.2f}", "samples": n,
            "markets": len({r["market_id"] for r in rows}),
            "mean_price": round(mean_price, 4) if mean_price is not None else None,
            "win_rate": round(rate, 4) if rate is not None else None, "win_rate_ci95": ci,
            "bias": round(rate - mean_price, 4) if n else None, "verdict": verdict,
        })
    n = len(points)
    brier = sum((p["price"] - p["payoff"]) ** 2 for p in points) / n if n else None
    eps = 1e-4
    logloss = (-sum(p["payoff"] * math.log(min(max(p["price"], eps), 1 - eps))
                    + (1 - p["payoff"]) * math.log(1 - min(max(p["price"], eps), 1 - eps)) for p in points) / n
               if n else None)
    base_rate = sum(p["payoff"] for p in points) / n if n else None
    baseline_brier = sum((base_rate - p["payoff"]) ** 2 for p in points) / n if n else None
    return {
        "samples": n, "markets": len({p["market_id"] for p in points}),
        "brier": round(brier, 4) if brier is not None else None,
        "baseline_brier": round(baseline_brier, 4) if baseline_brier is not None else None,
        "log_loss": round(logloss, 4) if logloss is not None else None,
        "bins": bins,
    }


def pooled(points: list[dict[str, Any]], lo: float, hi: float) -> dict[str, Any]:
    rows = [p for p in points if lo <= p["price"] < hi]
    n = len(rows)
    wins = sum(1 for r in rows if r["payoff"] >= 0.5)
    mean_price = sum(r["price"] for r in rows) / n if n else None
    ci = wilson(wins, n)
    return {"range": f"{lo:.2f}-{hi:.2f}", "samples": n, "markets": len({r["market_id"] for r in rows}),
            "mean_price": round(mean_price, 4) if n else None, "win_rate": round(wins / n, 4) if n else None,
            "win_rate_ci95": ci, "bias": round(wins / n - mean_price, 4) if n else None,
            "verdict": ("FEW_SAMPLES" if n < MIN_BIN_SAMPLE else "OVERPRICED" if ci[1] < mean_price
                        else "UNDERPRICED" if ci[0] > mean_price else "CONSISTENT")}


def segment_points(points: list[dict[str, Any]], segment: str | None) -> list[dict[str, Any]]:
    from .segments import segment_of
    if not segment:
        return points
    return [p for p in points if segment_of(p.get("question"), p.get("neg_risk")) == segment]


def build_calibration(store, horizon_hours: float = DEFAULT_HORIZON, min_volume: float = 0,
                      since: str | None = None, segment: str | None = None) -> dict[str, Any]:
    from .segments import SEGMENTS, segment_of
    all_points = store.calibration_points(horizon_hours, min_volume, since)
    points = segment_points(all_points, segment)
    table = calibration_table(points)
    by_segment = {}
    for seg in SEGMENTS:
        sp = [p for p in all_points if segment_of(p.get("question"), p.get("neg_risk")) == seg]
        by_segment[seg] = {"samples": len(sp), "markets": len({p["market_id"] for p in sp}),
                           "longshots": pooled(sp, 0.0, 0.10), "favourites": pooled(sp, 0.90, 1.0001)}
    return {
        "calibration_version": CALIBRATION_VERSION, "horizon_hours": horizon_hours, "min_volume": min_volume,
        "since": since, "segment": segment or "all", "by_segment": by_segment, **table,
        "longshots": pooled(points, 0.0, 0.10), "favourites": pooled(points, 0.90, 1.0001),
        "dataset": store.calibration_stats(),
        "note": ("Past prices of resolved markets versus what happened. OVERPRICED/UNDERPRICED are measured "
                 "biases, not executable edges: live asks, spreads and fees decide that (8B)."),
    }


def render_calibration_markdown(cal: dict[str, Any]) -> str:
    def pc(x):
        return "—" if x is None else f"{x * 100:.1f}%"

    def pp(x):
        return "—" if x is None else f"{x * 100:+.1f}pp"
    lines = [f"## Market calibration — price {cal['horizon_hours']:g}h before resolution", "",
             f"{cal['samples']} samples from {cal['markets']} resolved markets · Brier {cal['brier']} "
             f"(no-skill {cal['baseline_brier']})", "",
             "| Price bin | Samples | Markets | Mean price | Actually won | 95% CI | Bias | Verdict |",
             "|---|---|---|---|---|---|---|---|"]
    for b in cal["bins"]:
        ci = b["win_rate_ci95"]
        lines.append(f"| {b['bin']} | {b['samples']} | {b['markets']} | {pc(b['mean_price'])} | {pc(b['win_rate'])} | "
                     f"{'—' if not ci else pc(ci[0]) + ' … ' + pc(ci[1])} | "
                     f"{pp(b['bias'])} | "
                     f"{b['verdict']} |")
    for key, label in (("longshots", "Long shots (< 10¢)"), ("favourites", "Favourites (≥ 90¢)")):
        g = cal[key]
        lines.append("")
        lines.append(f"**{label}:** {g['samples']} samples, priced {pc(g['mean_price'])}, won {pc(g['win_rate'])} "
                     f"→ {g['verdict']}")
    return "\n".join(lines) + "\n"
