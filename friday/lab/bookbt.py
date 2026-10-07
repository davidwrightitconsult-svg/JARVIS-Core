"""
FRIDAY book-strategy backtester (Phase 11B): runs any registered rule over
hourly price series and reports what it would have earned after costs.

How a trade is simulated (every choice is the less flattering one):
- The rule decides at the close of a bar and sees nothing later. The entry is
  filled at the NEXT bar's price, never the price that triggered it.
- LONG buys the outcome at price + cost. SHORT buys the opposite outcome,
  which costs (1 - price) + cost. `cost` is the half-spread paid per side.
- The stop is checked at each close and filled at that close, which is at or
  beyond the stop (a gap through the stop is paid in full).
- A target is filled at the target, never better.
- Other exits (the rule's own, the time limit, the end of the series) fill at
  that bar's close. No position is ever held to resolution for a windfall.
- One position per market per strategy at a time.

Results are per share, in price units (0.01 = one cent), and in R (the stop
distance fixed in the registry). Confidence intervals are clustered by the
day a trade was entered, because trades opened on the same day move together.

The verdict rule is fixed here, before any real data is tested (Phase 11C):
eleven rules are tested at once, so the development bar is the 99.5% level.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from friday.strategies import LONG, STRATEGIES, Strategy, should_exit, signal
from friday.strategies.registry import MAX_PRICE, MIN_PRICE

BOOKBT_VERSION = "11B.1"
HOLDOUT_FRACTION = 0.30       # the most recently resolved 30% of markets, never looked at during development
Z_DEVELOPMENT = 2.81          # two-sided 99.5%: 11 rules tested together (Bonferroni on 5%)
Z_HOLDOUT = 1.96              # 95%, applied only to a rule that already passed development
MIN_TRADES_DEV, MIN_DAYS_DEV = 200, 30
MIN_TRADES_HOLDOUT, MIN_DAYS_HOLDOUT = 60, 10
COSTS = (0.0, 0.005, 0.01, 0.02)


@dataclass(frozen=True)
class BookSettings:
    cost_per_side: float = 0.01        # half-spread paid on entry and again on exit

    def __post_init__(self):
        if not (isinstance(self.cost_per_side, (int, float)) and 0 <= self.cost_per_side <= 0.10):
            raise ValueError("cost_per_side must be between 0 and 0.10")


def trades_for(strategy: Strategy, closes: Sequence[float], hours: Sequence[int] | None = None,
               market_id: str = "") -> list[dict[str, Any]]:
    """Every trade the rule would have made on one series. Gross of costs (`gross`, per share)."""
    ex, out, n = strategy.exit, [], len(closes)
    if strategy.fn is None or ex is None:
        return out
    i = 0
    while i < n - 1:
        side = signal(strategy, closes[:i + 1])
        if not side:
            i += 1
            continue
        e = i + 1
        if e >= n - 1:                                 # no room left to hold a position
            break
        entry = closes[e]
        if not MIN_PRICE <= entry <= MAX_PRICE:        # the price left the tradeable band before the fill
            i += 1
            continue
        d = 1.0 if side == LONG else -1.0
        j, reason, gross = e, "end of series", 0.0
        while True:
            j += 1
            move = round(d * (closes[j] - entry), 9)
            if move <= -ex.stop:
                gross, reason = move, "stop"
            elif ex.target is not None and move >= ex.target:
                gross, reason = ex.target, "target"
            elif should_exit(strategy, closes[:j + 1], side):
                gross, reason = move, "rule"
            elif j - e >= ex.max_hold:
                gross, reason = move, "time"
            elif j == n - 1:
                gross, reason = move, "end of series"
            else:
                continue
            break
        out.append({"strategy": strategy.key, "market_id": market_id, "side": side, "entry_bar": e, "exit_bar": j,
                    "entry": entry, "exit": closes[j], "bars_held": j - e, "reason": reason, "gross": round(gross, 6),
                    "day": (hours[e] // 24) if hours else e // 24, "stop": ex.stop})
        i = max(j, i + 1)                              # flat again from the exit bar
    return out


def _clustered(values: list[float], days: list[int], z: float) -> tuple[float, list[float] | None, int]:
    n = len(values)
    mean = statistics.fmean(values)
    groups: dict[int, list[float]] = {}
    for v, d in zip(values, days):
        groups.setdefault(d, []).append(v)
    g = len(groups)
    if g < 2:
        return mean, None, g
    resid = [sum(v) - mean * len(v) for v in groups.values()]
    se = math.sqrt(g / (g - 1) * sum(x * x for x in resid)) / n
    return mean, [mean - z * se, mean + z * se], g


def summarise(trades: list[dict[str, Any]], cost_per_side: float, z: float = Z_HOLDOUT) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0, "markets": 0, "days": 0, "avg_net": None, "interval": None, "avg_gross": None,
                "avg_r": None, "win_rate": None, "total_net": 0.0, "max_drawdown": 0.0, "exits": {}, "long": 0, "short": 0}
    nets = [t["gross"] - 2 * cost_per_side for t in trades]
    mean, interval, days = _clustered(nets, [t["day"] for t in trades], z)
    path = peak = drawdown = 0.0
    for t, x in sorted(zip(trades, nets), key=lambda p: (p[0]["day"], p[0]["market_id"], p[0]["entry_bar"])):
        path += x
        peak = max(peak, path)
        drawdown = max(drawdown, peak - path)
    exits: dict[str, int] = {}
    for t in trades:
        exits[t["reason"]] = exits.get(t["reason"], 0) + 1
    return {"trades": n, "markets": len({t["market_id"] for t in trades}), "days": days,
            "avg_net": round(mean, 5), "interval": [round(x, 5) for x in interval] if interval else None,
            "avg_gross": round(statistics.fmean(t["gross"] for t in trades), 5),
            "avg_r": round(statistics.fmean(x / t["stop"] for t, x in zip(trades, nets)), 4),
            "win_rate": round(sum(1 for x in nets if x > 0) / n, 3), "total_net": round(sum(nets), 4),
            "max_drawdown": round(drawdown, 4), "exits": exits,
            "long": sum(1 for t in trades if t["side"] == LONG), "short": sum(1 for t in trades if t["side"] != LONG)}


def split(series: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(development, holdout): by the time the outcome became known. The newest 30% is the holdout."""
    ordered = sorted(series, key=lambda m: (str(m["reference_time"]), str(m["market_id"])))
    cut = len(ordered) - int(round(len(ordered) * HOLDOUT_FRACTION))
    return ordered[:cut], ordered[cut:]


def verdict(dev: dict[str, Any], holdout: dict[str, Any] | None) -> str:
    """Fixed before any real test. `dev` uses the 99.5% interval, `holdout` the 95% one."""
    if dev["trades"] < MIN_TRADES_DEV or dev["days"] < MIN_DAYS_DEV or dev["interval"] is None:
        return f"INSUFFICIENT_DATA (needs {MIN_TRADES_DEV} trades on {MIN_DAYS_DEV} days in development)"
    if dev["interval"][1] < 0:
        return "FAILED (loses money after costs in development)"
    if dev["interval"][0] <= 0:
        return "FAILED (no edge distinguishable from zero in development)"
    if holdout is None:
        return "PASSED_DEVELOPMENT (holdout not yet run)"
    if holdout["trades"] < MIN_TRADES_HOLDOUT or holdout["days"] < MIN_DAYS_HOLDOUT or holdout["interval"] is None:
        return f"INSUFFICIENT_DATA (needs {MIN_TRADES_HOLDOUT} trades on {MIN_DAYS_HOLDOUT} days in the holdout)"
    if holdout["interval"][0] <= 0:
        return "FAILED (did not hold up on the untouched holdout)"
    return "PASSED (development and holdout; paper trading must confirm)"


def run_strategy(strategy: Strategy, series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades = []
    for m in series:
        trades.extend(trades_for(strategy, m["closes"], m.get("t"), str(m["market_id"])))
    return trades


def run_book_backtest(series: list[dict[str, Any]], settings: BookSettings | None = None,
                      strategies: Sequence[Strategy] | None = None, z: float = Z_DEVELOPMENT) -> dict[str, Any]:
    """Run each signal rule over the given series. The caller chooses which series (development or
    holdout); this function never splits or peeks."""
    s = settings or BookSettings()
    rows = []
    for st in (strategies or STRATEGIES):
        if st.kind != "SIGNAL":
            continue
        trades = run_strategy(st, series)
        summary = summarise(trades, s.cost_per_side, z)
        summary["by_cost"] = {f"{c * 100:g}c": summarise(trades, c, z)["avg_net"] for c in COSTS}
        rows.append({"strategy": st.key, "name": st.name, **summary})
    return {"bookbt_version": BOOKBT_VERSION, "cost_per_side": s.cost_per_side, "z": z, "markets": len(series),
            "bars": sum(len(m["closes"]) for m in series), "strategies": rows, "execution_locked": True}


def render(result: dict[str, Any], title: str = "") -> str:
    lines = [f"{title or 'Book strategies'}: {result['markets']} markets, {result['bars']} hourly bars, "
             f"cost {result['cost_per_side'] * 100:g}c per side",
             f"{'strategy':<22} {'trades':>6} {'mkts':>5} {'days':>4} {'win':>5} {'net c/share':>11} {'interval (c)':>17} "
             f"{'gross c':>8} {'avg R':>6}"]
    for r in result["strategies"]:
        if not r["trades"]:
            lines.append(f"{r['name']:<22} {0:>6}")
            continue
        iv = f"{r['interval'][0] * 100:+.2f} to {r['interval'][1] * 100:+.2f}" if r["interval"] else "n/a"
        lines.append(f"{r['name']:<22} {r['trades']:>6} {r['markets']:>5} {r['days']:>4} {r['win_rate']:>5.0%} "
                     f"{r['avg_net'] * 100:>+11.2f} {iv:>17} {r['avg_gross'] * 100:>+8.2f} {r['avg_r']:>+6.2f}")
    return "\n".join(lines)
