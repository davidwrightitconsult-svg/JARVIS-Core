"""
FRIDAY Validation Lab — pricing inconsistencies in FRIDAY's own recorded order books (Phase 9C).

A two-outcome market pays exactly $1 to one side. If the best ask of YES
plus the best ask of NO is below $1, buying both locks in the difference
whatever happens — no forecast needed. This scans every recorded run for
that, using real recorded asks (not history prices):

  locked profit = 1 - (ask_yes + ask_no) - 2 x fee   per $1 pair

and reports how often it appeared, how large it was, how long it lasted
(consecutive runs, 15 minutes apart) and how much depth sat behind it.
Markets of an excluded segment are ignored.

Honest limits: it sees one snapshot every 15 minutes, so a gap that opens
and closes between scans is invisible, and a gap it does see may be gone
before an order could be placed; depth is "within 2c of the mid", not the
size at the best ask; both legs must fill. Multi-candidate events (buy
every candidate for under $1) are not covered — FRIDAY does not yet record
which markets belong to one event. A finding here is a candidate for paper
trading, never an authorisation.
"""

from __future__ import annotations

import statistics
from typing import Any

from jarvis.market.segments import excluded_segments, segment_of

INCONSISTENCY_VERSION = "9C.1"
THRESHOLDS = (0.005, 0.01, 0.02)
MIN_EVENTS = 30
MIN_DAYS = 5


def scan_inconsistencies(store, days: float = 30, fee: float = 0.0, excluded: frozenset[str] | None = None
                         ) -> dict[str, Any]:
    excluded = excluded_segments() if excluded is None else frozenset(excluded)
    rows = [r for r in store.pair_sums(days) if segment_of(r.get("question"), r.get("neg_risk")) not in excluded]
    runs = sorted({r["run_id"] for r in rows})
    order = {run: i for i, run in enumerate(runs)}
    by_level = {}
    for t in THRESHOLDS:
        hits = [r for r in rows if 1 - r["ask_sum"] - 2 * fee >= t - 1e-9]
        # an "episode" = the same market at or beyond the threshold on consecutive runs
        episodes, last = [], {}
        for r in hits:
            i = order[r["run_id"]]
            ep = last.get(r["market_id"])
            if ep is not None and i == ep["end"] + 1:
                ep["end"], ep["runs"] = i, ep["runs"] + 1
                ep["best"] = max(ep["best"], 1 - r["ask_sum"] - 2 * fee)
            else:
                ep = {"market_id": r["market_id"], "question": (r.get("question") or "")[:90], "start": i, "end": i,
                      "runs": 1, "best": 1 - r["ask_sum"] - 2 * fee, "observed_at": r["observed_at"],
                      "depth_usd": r["depth_usd"]}
                episodes.append(ep)
                last[r["market_id"]] = ep
        profits = [1 - r["ask_sum"] - 2 * fee for r in hits]
        by_level[f"{t * 100:g}c"] = {
            "observations": len(hits), "episodes": len(episodes), "markets": len({r["market_id"] for r in hits}),
            "days": len({r["observed_at"][:10] for r in hits}),
            "median_locked": round(statistics.median(profits), 4) if profits else None,
            "max_locked": round(max(profits), 4) if profits else None,
            "median_runs": statistics.median(e["runs"] for e in episodes) if episodes else None,
            "seen_once_only": sum(1 for e in episodes if e["runs"] == 1),
            "median_depth_usd": round(statistics.median(r["depth_usd"] or 0 for r in hits), 0) if hits else None,
            "examples": [{k: (round(e[k], 4) if k == "best" else e[k]) for k in
                          ("observed_at", "question", "best", "runs", "depth_usd")}
                         for e in sorted(episodes, key=lambda e: -e["best"])[:5]],
        }
    head = by_level["1c"]
    if not rows:
        verdict = "NO_DATA (FRIDAY has recorded no two-outcome order books in the window)"
    elif head["episodes"] >= MIN_EVENTS and head["days"] >= MIN_DAYS:
        verdict = "PRESENT (candidate for paper trading: both legs must fill at the recorded asks)"
    elif head["episodes"]:
        verdict = f"RARE ({head['episodes']} episode(s) of >= 1c on {head['days']} day(s); not enough to build on)"
    else:
        verdict = "NOT_OBSERVED (no recorded moment where both outcomes cost under $0.99 together)"
    sums = [r["ask_sum"] for r in rows]
    return {"inconsistency_version": INCONSISTENCY_VERSION, "days": days, "fee": fee,
            "observations": len(rows), "runs": len(runs), "markets": len({r["market_id"] for r in rows}),
            "ask_sum": {"min": round(min(sums), 4), "median": round(statistics.median(sums), 4)} if sums else None,
            "bid_sum_above_1": sum(1 for r in rows if r["bid_sum"] > 1 + 1e-9),
            "by_threshold": by_level, "verdict": verdict,
            "note": "Recorded asks, one snapshot per scan. A gap seen is not a gap that could be traded."}


def render_inconsistencies(r: dict[str, Any]) -> str:
    lines = [f"PRICING INCONSISTENCIES {r['inconsistency_version']} — buy both outcomes for under $1 (recorded asks)",
             f"{r['observations']} market-observations · {r['runs']} scans · {r['markets']} markets · last {r['days']:g} days"
             + (f" · ask sum: lowest {r['ask_sum']['min']}, median {r['ask_sum']['median']}" if r["ask_sum"] else ""),
             f"bid sum above $1 (sell both): {r['bid_sum_above_1']} observations", ""]
    for level, v in r["by_threshold"].items():
        lines.append(f"  locked >= {level:<5} {v['observations']:>5} observations · {v['episodes']:>4} episodes · "
                     f"{v['markets']:>3} markets · {v['days']:>2} days · median {v['median_locked']} · max {v['max_locked']} · "
                     f"median length {v['median_runs']} scan(s) · seen once {v['seen_once_only']} · "
                     f"median depth ${v['median_depth_usd']}")
        for e in v["examples"][:3]:
            lines.append(f"      {e['observed_at'][:16]} locked {e['best'] * 100:+.1f}c for {e['runs']} scan(s) "
                         f"depth ${e['depth_usd']}: {e['question'][:60]}")
    lines += ["", "VERDICT: " + r["verdict"], r["note"]]
    return "\n".join(lines)
