"""
FRIDAY Validation Lab — robustness of the calibration edge (Phase 9B).

One backtest number is not evidence. This module asks the questions a
sceptic would:

  anchor     the same walk-forward test on prices anchored to the SCHEDULED
             end date (what FRIDAY knows live) and to the ACTUAL resolution
             (8A; knows when a market resolved). The difference is the leak
             Phase 9A's +11c result contained.
  horizons   does the edge hold at 24h, 72h, 168h, 336h and 720h before the
             end, or only at one of them?
  thresholds does the result survive a different minimum edge (1/2/3/5c),
             or does it depend on one setting?
  real asks  for markets whose order book FRIDAY itself recorded at the
             decision time: how far above the history price was the real
             ask, and what does the rule make when it pays that ask?

`robust` is true only if the end-anchored test PASSES_OUT_OF_SAMPLE at two
or more horizons and at two or more thresholds on the headline horizon.
It is a summary for the Phase 9C validation gate, not an authorisation.
"""

from __future__ import annotations

import dataclasses
import statistics
from datetime import timedelta
from typing import Any

from jarvis.quant.fair import HORIZONS, EdgeSettings

from .backtest import BacktestSettings, _walk, load_markets, run_backtest, summarise

ROBUSTNESS_VERSION = "9B.1"
EDGE_GRID = (0.01, 0.02, 0.03, 0.05)
RESOLUTION_HORIZONS = (24.0, 72.0, 168.0)


def _line(r: dict[str, Any]) -> dict[str, Any]:
    o = r["out_of_sample"]
    return {"markets": r["dataset"]["markets"], "in_band": r["dataset"]["in_price_band"], "bets": o["bets"],
            "days": o["days"], "win_rate": o["win_rate"], "avg_net": o["avg_net"],
            "ci95_net_clustered": o["ci95_net_clustered"], "return_on_cost": o["return_on_cost"],
            "verdict": o["verdict"], "in_sample_net": r["in_sample"]["avg_net"]}


def recorded_asks(store, markets: list[dict[str, Any]], tolerance_hours: float = 3.0
                  ) -> tuple[dict[str, tuple[float, float]], list[float]]:
    """({market_id: (ask0, ask1)}, [ask - history price]) for markets FRIDAY recorded at the decision time."""
    asks, gaps = {}, []
    span = store.snapshot_span() if hasattr(store, "snapshot_span") else None
    if span is None:
        return asks, gaps
    for m in markets:
        at = m["t"].isoformat(timespec="seconds")
        if at < span[0] or (m["t"] - timedelta(hours=tolerance_hours)).isoformat(timespec="seconds") > span[1]:
            continue                                   # FRIDAY was not recording then
        quotes = [store.quote_near(t[3], at, tolerance_hours) for t in m["tokens"]]
        if any(q is None or q["best_ask"] is None for q in quotes):
            continue
        asks[m["market_id"]] = (quotes[0]["best_ask"], quotes[1]["best_ask"])
        gaps.extend(q["best_ask"] - t[0] for q, t in zip(quotes, m["tokens"]))
    return asks, gaps


def run_robustness(store, base: BacktestSettings | None = None, excluded: frozenset[str] | None = None,
                   headline_horizon: float = 24.0) -> dict[str, Any]:
    base = base or BacktestSettings()

    def settings(**kw):
        return dataclasses.replace(base, **kw)

    by_horizon = {f"{h:g}h": _line(run_backtest(store, settings(horizon_hours=h, anchor="end"), excluded, sensitivity=False))
                  for h in HORIZONS}
    leak = {}
    for h in RESOLUTION_HORIZONS:
        res = _line(run_backtest(store, settings(horizon_hours=h, anchor="resolution"), excluded, sensitivity=False))
        end = by_horizon[f"{h:g}h"]
        leak[f"{h:g}h"] = {"resolution_anchor": res, "end_anchor": end,
                           "net_difference": None if res["avg_net"] is None or end["avg_net"] is None
                           else round(res["avg_net"] - end["avg_net"], 4)}
    thresholds = {}
    for e in EDGE_GRID:
        edge = dataclasses.replace(base.edge, min_edge=e)
        thresholds[f"{e * 100:g}c"] = _line(run_backtest(
            store, settings(horizon_hours=headline_horizon, anchor="end", edge=edge), excluded, sensitivity=False))

    real = {}
    for h in HORIZONS:
        s = settings(horizon_hours=h, anchor="end")
        markets, _ = load_markets(store, h, excluded, "end")
        asks, gaps = recorded_asks(store, markets)
        if not asks:
            real[f"{h:g}h"] = {"recorded_markets": 0}
            continue
        at_real = summarise(_walk(markets, s, asks=asks)[0])
        subset = [m for m in markets if m["market_id"] in asks]
        at_assumed = summarise([b for b in _walk(markets, s)[0] if b["market_id"] in asks])
        real[f"{h:g}h"] = {
            "recorded_markets": len(subset),
            "ask_minus_history_price": {"median": round(statistics.median(gaps), 4),
                                        "p90": round(sorted(gaps)[int(0.9 * (len(gaps) - 1))], 4),
                                        "max": round(max(gaps), 4)},
            "at_recorded_asks": {k: at_real[k] for k in ("bets", "days", "win_rate", "avg_net", "ci95_net_clustered",
                                                         "verdict")},
            "same_markets_at_assumed_asks": {k: at_assumed[k] for k in ("bets", "win_rate", "avg_net")},
        }

    passes_h = [h for h, v in by_horizon.items() if v["verdict"].startswith("PASSES")]
    passes_t = [t for t, v in thresholds.items() if v["verdict"].startswith("PASSES")]
    negative = [h for h, v in by_horizon.items() if v["verdict"].startswith("NEGATIVE")]
    robust = len(passes_h) >= 2 and len(passes_t) >= 2 and not negative
    if robust:
        conclusion = (f"ROBUST ON ASSUMED COSTS: passes out of sample at {', '.join(passes_h)} and at minimum edges "
                      f"{', '.join(passes_t)}. Real asks (paper trading) must still confirm it.")
    elif negative:
        conclusion = f"NOT SUPPORTED: negative out of sample at {', '.join(negative)}."
    elif passes_h or passes_t:
        conclusion = ("FRAGILE: passes only at " + (", ".join(passes_h) or "no horizon") + " / thresholds "
                      + (", ".join(passes_t) or "none") + " — not enough to rely on.")
    else:
        conclusion = "NOT SUPPORTED: no horizon passes out of sample on end-anchored prices."
    return {"robustness_version": ROBUSTNESS_VERSION, "settings": base.as_dict(),
            "end_anchor": store.end_anchor_stats() if hasattr(store, "end_anchor_stats") else None,
            "by_horizon": by_horizon, "leak": leak, "thresholds": {"horizon": f"{headline_horizon:g}h", **thresholds},
            "real_asks": real, "robust": robust, "conclusion": conclusion,
            "note": "End-anchored prices are what FRIDAY knows live. Nothing here authorises any trade."}


def render_robustness(r: dict[str, Any]) -> str:
    def c(x):
        return "  —  " if x is None else f"{x * 100:+.2f}"

    def ci(x):
        return "—" if not x else f"{x[0] * 100:+.2f} … {x[1] * 100:+.2f}"

    def row(name, v):
        win = "—" if v["win_rate"] is None else f"{v['win_rate'] * 100:.0f}%"
        return (f"  {name:<8} {v['markets']:>5} mkts {v['in_band']:>5} in band {v['bets']:>4} bets {v['days']:>3} days "
                f"win {win:>4} net {c(v['avg_net'])}c CI {ci(v['ci95_net_clustered']):<17} → {v['verdict'].split(' (')[0]}")

    a = r["end_anchor"] or {}
    lines = [f"ROBUSTNESS {r['robustness_version']} — calibration edge",
             f"end-anchored data: {a.get('markets')} · pending {a.get('pending')} · markets by horizon "
             f"{a.get('markets_by_horizon')}", "",
             "END-ANCHORED (what FRIDAY knows live), out of sample, by horizon before the scheduled end:"]
    lines += [row(h, v) for h, v in r["by_horizon"].items()]
    lines += ["", "THE LEAK — same test on prices anchored to the actual resolution (9A) vs the scheduled end:"]
    for h, v in r["leak"].items():
        lines.append(f"  {h:<5} resolution-anchored net {c(v['resolution_anchor']['avg_net'])}c on "
                     f"{v['resolution_anchor']['bets']} bets · end-anchored net {c(v['end_anchor']['avg_net'])}c on "
                     f"{v['end_anchor']['bets']} bets · difference {c(v['net_difference'])}c")
    t = dict(r["thresholds"])
    lines += ["", f"MINIMUM EDGE (end-anchored, {t.pop('horizon')}):"] + [row(k, v) for k, v in t.items()]
    lines += ["", "REAL ASKS — markets whose order book FRIDAY recorded at the decision time:"]
    for h, v in r["real_asks"].items():
        if not v["recorded_markets"]:
            lines.append(f"  {h:<5} none recorded yet")
            continue
        g, x, y = v["ask_minus_history_price"], v["at_recorded_asks"], v["same_markets_at_assumed_asks"]
        lines.append(f"  {h:<5} {v['recorded_markets']} markets · recorded ask above the history price: median "
                     f"{g['median'] * 100:+.1f}c, 90% within {g['p90'] * 100:+.1f}c, worst {g['max'] * 100:+.1f}c · "
                     f"at recorded asks {x['bets']} bets net {c(x['avg_net'])}c · at assumed asks {y['bets']} bets "
                     f"net {c(y['avg_net'])}c")
    lines += ["", "CONCLUSION: " + r["conclusion"], r["note"]]
    return "\n".join(lines)
