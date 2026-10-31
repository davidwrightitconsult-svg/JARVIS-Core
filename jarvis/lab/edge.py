"""
FRIDAY Validation Lab — the edge report (Phase 16A): does this strategy know anything, and does
it make money? Those are two different questions and the answer can be yes to one and no to the
other.

### The inconsistency this fixes
The 7C scorecard scores a signal as `net = exit_bid - entry_ask` with no cost term, and the 9C gate
judges strategies on those numbers. Since 14B the paper engine charges the taker fee on both sides
of every fill. So the gate could approve a strategy for paper trading whose entire measured edge is
smaller than the fee the engine would then charge it. The two halves of the system were doing
different arithmetic. Everything here is computed after fees, using the same cost model the engine
trades with.

### The null hypothesis is not zero
For any one signal, the two directions are mirror images at the same two moments:

    UP     net = exit_bid - entry_ask
    DOWN   net = entry_bid - exit_ask
    sum        = -(entry_spread + exit_spread)

So a coin flip — a position taken with no information at all — loses **half the two spreads, every
time**. That is the cost of participating, and it is the bar a strategy must clear before it can be
said to know anything. Testing `net > 0` conflates two questions; this module separates them:

    net_after_fees > 0            THIS MAKES MONEY        (the commercial bar)
    net_after_fees > coin_flip    THIS KNOWS SOMETHING    (the information bar)

A strategy that clears the information bar but not the commercial one has real predictive content
being eaten by costs. That is actionable — trade tighter spreads, cheaper fee categories, longer
horizons — in a way that "it loses money" is not. A strategy that clears neither has nothing.

Note that `edge_vs_coinflip` is not a restatement of `net`: the spread varies signal to signal, so
the two tests rank strategies differently and can disagree.

### What this is not
Not a backtest and not a promotion. The 9B walk-forward test and the 9C gate remain the authority
on whether a strategy may trade at all. This report says what the scored record actually shows,
after costs, against the right baseline — and the 16B executor uses it to notice when a strategy
that was once approved has stopped working.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from jarvis.market.segments import category_of, segment_of
from jarvis.paper.costs import CostSettings, taker_fee
from jarvis.quant.scoring import cost_of

EDGE_VERSION = "16A.1"
MIN_SAMPLE = 30                 # below this the report says so; the gate's own bar is 100
SCORED = ("SCORED", "RESOLVED_BEFORE_EXIT")


def fee_category(row: dict[str, Any]) -> str:
    """The same classification the paper engine uses to price a fill, so the report and the trade
    agree on what a signal costs."""
    question = row.get("question")
    if not question:
        return "unknown"
    if segment_of(question, row.get("neg_risk")) == "sports":
        return "sports"
    return category_of(question)


def prices_of(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """(entry, exit) price per $1-payoff share of the thing actually bought.

    UP buys the token: in at its ask, out at its bid. DOWN buys the other outcome, which costs
    `1 - bid` of this one and sells for `1 - ask`. Both are the price of a share that pays $1."""
    if row.get("direction") == "UP":
        return row.get("best_ask"), row.get("exit_bid")
    bid, ask = row.get("best_bid"), row.get("exit_ask")
    return (None if bid is None else 1.0 - bid), (None if ask is None else 1.0 - ask)


def round_trip_fee(row: dict[str, Any], settings: CostSettings) -> float:
    """The taker fee on both legs, per share. Zero when a price is missing: never guessed."""
    entry, exit_ = prices_of(row)
    if entry is None or exit_ is None:
        return 0.0
    rate = settings.rate_for(fee_category(row))
    return round(taker_fee(1.0, entry, rate) + taker_fee(1.0, exit_, rate), 8)


def mirror_net(row: dict[str, Any]) -> float | None:
    """What the OPPOSITE direction would have netted, at the same two moments. The paired control:
    same market, same entry, same exit, no information."""
    eb, ea = row.get("best_bid"), row.get("best_ask")
    xb, xa = row.get("exit_bid"), row.get("exit_ask")
    if None in (eb, ea, xb, xa):
        return None
    return round((eb - xa) if row.get("direction") == "UP" else (xb - ea), 6)


def enrich(row: dict[str, Any], settings: CostSettings) -> dict[str, Any] | None:
    """One scored signal, with its fee, its mirror and the coin-flip baseline. None when the row
    cannot carry the comparison."""
    if row.get("status") not in SCORED or row.get("net") is None:
        return None
    mirror = mirror_net(row)
    if mirror is None:
        return None
    fee = round_trip_fee(row, settings)
    mirror_row = {**row, "direction": "DOWN" if row.get("direction") == "UP" else "UP"}
    mirror_fee = round_trip_fee(mirror_row, settings)
    net_after = round(float(row["net"]) - fee, 6)
    mirror_after = round(mirror - mirror_fee, 6)
    # The average of the two directions: what no information is worth here, after costs.
    coinflip = round((net_after + mirror_after) / 2.0, 6)
    cost = cost_of(row)
    return {"signal_id": row.get("signal_id"), "strategy": row.get("strategy"),
            "strategy_version": row.get("strategy_version"), "observed_at": row.get("observed_at"),
            "market_id": row.get("market_id"), "direction": row.get("direction"),
            "category": fee_category(row), "cost": cost,
            "net": round(float(row["net"]), 6), "fee": fee, "net_after_fees": net_after,
            "mirror_net_after_fees": mirror_after, "coinflip_after_fees": coinflip,
            "edge_vs_coinflip": round(net_after - coinflip, 6),
            "return_on_cost_after_fees": round(net_after / cost, 6) if cost else None,
            "resolution_net": row.get("resolution_net")}


def _stats(values: list[float]) -> dict[str, Any]:
    """Mean with a 95% interval. `beats_zero` requires the whole interval above zero: a positive
    mean whose interval straddles zero is a sample that cannot tell itself from noise."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "ci95": None, "beats_zero": False}
    mean = statistics.fmean(values)
    if n < 2:
        return {"n": n, "mean": round(mean, 6), "ci95": None, "beats_zero": False}
    sd = statistics.stdev(values)
    half = 1.96 * sd / math.sqrt(n)
    lo, hi = mean - half, mean + half
    return {"n": n, "mean": round(mean, 6), "ci95": [round(lo, 6), round(hi, 6)],
            "sd": round(sd, 6), "beats_zero": lo > 0}


MAKES_MONEY = "MAKES_MONEY"
KNOWS_SOMETHING = "KNOWS_SOMETHING_BUT_COSTS_EAT_IT"
NOTHING = "NO_EDGE"
ANTI = "WORSE_THAN_A_COIN_FLIP"
LOSING = "NEGATIVE_AFTER_COSTS"
UNCLEAR = "INCONCLUSIVE"
THIN = "INSUFFICIENT_DATA"


def _verdict(n: int, money: dict[str, Any], info: dict[str, Any], min_sample: int,
             mirror: dict[str, Any] | None = None) -> tuple[str, str]:
    if n < min_sample:
        return THIN, f"{n} scored signals; {min_sample - n} more before this means anything"
    if money["beats_zero"]:
        return MAKES_MONEY, (f"net of fees the mean is {money['mean']:+.4f} a share with the whole "
                             f"interval above zero")
    if info["beats_zero"]:
        return KNOWS_SOMETHING, (f"it beats a coin flip by {info['mean']:+.4f} a share, but nets "
                                 f"{money['mean']:+.4f} after fees: the information is real and the "
                                 f"costs are eating it")
    # The information bar is judged BEFORE the commercial one. A strategy that cannot beat a coin
    # flip has nothing, and saying only that it lost money would name the symptom over the cause:
    # losing money can be fixed by trading cheaper markets, knowing nothing cannot.
    if info["ci95"] and info["ci95"][1] < -1e-9:
        # Reliably worse than random is not the same as knowing nothing: the mirror of these
        # signals made money. That is a finding, not a recommendation -- the inverse is a DIFFERENT
        # strategy and needs its own forward record before anything may depend on it.
        other = (mirror or {}).get("mean")
        aside = f"; the opposite direction on the same signals made {other:+.4f}" if other is not None else ""
        return ANTI, (f"it loses to a coin flip by {-info['mean']:.4f} a share "
                      f"(ci95 {info['ci95']}){aside}. Treat that as a lead to test, not a strategy "
                      f"to run: the inverse is a different strategy and needs its own record")
    if info["ci95"] and info["ci95"][1] <= 1e-9:
        return NOTHING, (f"it does not beat a coin flip on the same signals "
                         f"({info['mean']:+.4f}, ci95 {info['ci95']}): no information to trade on")
    if money["ci95"] and money["ci95"][1] < 0:
        return LOSING, (f"net of fees the whole interval is below zero ({money['ci95']}), and the "
                        f"coin-flip comparison cannot rule out that there is no edge either")
    return UNCLEAR, "the interval straddles zero: not enough signal either way"


def report(rows: list[dict[str, Any]], settings: CostSettings | None = None,
           min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """`rows` are `MarketStore.outcomes()` rows: signals joined to their scored outcomes."""
    settings = settings or CostSettings()
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    enriched_total = 0
    for row in rows:
        e = enrich(row, settings)
        if e is None:
            continue
        enriched_total += 1
        by_strategy.setdefault(str(e["strategy"]), []).append(e)

    out: dict[str, Any] = {"edge_version": EDGE_VERSION, "min_sample": min_sample,
                           "scored_signals": enriched_total,
                           "fees": {"enabled": settings.enabled, "multiplier": settings.fee_multiplier},
                           "strategies": {}}
    for strategy, items in sorted(by_strategy.items()):
        money = _stats([i["net_after_fees"] for i in items])
        info = _stats([i["edge_vs_coinflip"] for i in items])
        gross = _stats([i["net"] for i in items])
        coinflip = _stats([i["coinflip_after_fees"] for i in items])
        mirror = _stats([i["mirror_net_after_fees"] for i in items])
        fees = [i["fee"] for i in items]
        verdict, reason = _verdict(len(items), money, info, min_sample, mirror)
        # A strategy whose signals all share one model version is one strategy; a mix is not, and
        # pooling them produces an interval about nothing.
        versions = sorted({str(i["strategy_version"]) for i in items})
        out["strategies"][strategy] = {
            "scored": len(items),
            "days": len({str(i["observed_at"])[:10] for i in items}),
            "versions": versions,
            "mixed_versions": len(versions) > 1,
            "gross_net": gross,                      # what the 7C scorecard reports
            "net_after_fees": money,                 # the commercial bar
            "coinflip_after_fees": coinflip,         # what no information is worth here
            "mirror_after_fees": mirror,             # the opposite direction, same signals
            "edge_vs_coinflip": info,                # the information bar
            "avg_fee_per_share": round(statistics.fmean(fees), 6) if fees else None,
            "fee_share_of_gross": (round(sum(fees) / abs(sum(i["net"] for i in items)), 4)
                                   if sum(i["net"] for i in items) else None),
            "win_rate_after_fees": round(sum(1 for i in items if i["net_after_fees"] > 0) / len(items), 3),
            "by_category": _by_category(items),
            "verdict": verdict, "reason": reason,
        }
    return out


def _by_category(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Split by fee category. The fee rate differs 0 to 0.07 across them, so a strategy can be
    profitable in geopolitics and losing in crypto on identical predictions."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        groups.setdefault(i["category"], []).append(i)
    return {cat: {"scored": len(g),
                  "net_after_fees": _stats([x["net_after_fees"] for x in g]),
                  "avg_fee_per_share": round(statistics.fmean([x["fee"] for x in g]), 6)}
            for cat, g in sorted(groups.items())}


def render(r: dict[str, Any]) -> str:
    lines = [f"EDGE REPORT {r['edge_version']} — after fees, against a coin flip on the same signals",
             f"{r['scored_signals']} scored signals · fees "
             f"{'on' if r['fees']['enabled'] else 'OFF'} (x{r['fees']['multiplier']:g})", ""]
    if not r["strategies"]:
        lines.append("  no scored signals carry both an entry and an exit book yet")
    for name, s in r["strategies"].items():
        money, info = s["net_after_fees"], s["edge_vs_coinflip"]
        lines.append(f"  {name}")
        lines.append(f"    {s['scored']:>5} scored over {s['days']} day(s)"
                     + ("  [MIXED VERSIONS: not one strategy]" if s["mixed_versions"] else ""))
        lines.append(f"    gross       {_fmt(s['gross_net'])}   (what 7C reports: no fees)")
        lines.append(f"    after fees  {_fmt(money)}   <- does it make money")
        lines.append(f"    coin flip   {_fmt(s['coinflip_after_fees'])}   (cost of participating)")
        lines.append(f"    vs coinflip {_fmt(info)}   <- does it know anything")
        lines.append(f"    fee {s['avg_fee_per_share']} a share · win rate {s['win_rate_after_fees']}")
        lines.append(f"    VERDICT: {s['verdict']} — {s['reason']}")
        lines.append("")
    return "\n".join(lines)


def _fmt(st: dict[str, Any]) -> str:
    if st["mean"] is None:
        return "no data"
    ci = f" ci95 [{st['ci95'][0]:+.4f}, {st['ci95'][1]:+.4f}]" if st["ci95"] else ""
    return f"{st['mean']:+.4f}{ci}"
