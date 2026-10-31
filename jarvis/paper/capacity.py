"""
Capacity (Phase 14B): at what size does this edge stop existing?

An edge is not a number, it is a number *at a size*. A strategy that makes 4c a share on $20 may
make nothing on $500 and lose money on $2000, because the extra size is filled further down the
book and the fee is charged on all of it. Reporting one PnL figure without the size it was earned
at is the same mistake as reporting a fill at the best price regardless of size — the mistake 14A
existed to fix, one level up.

So every closed paper trade now stores the book it entered against and the book it exited into
(`paper_plans.entry_book` / `exit_book`, recorded by the engine from 14B onward). This module
re-walks those real books at a ladder of larger stakes and reports where the net turns negative.

### What this holds fixed, and therefore what it cannot tell you
- **Timing.** The trade is re-run at the same two moments it really happened at. A larger position
  might have been stopped out earlier or held longer; that is not modelled.
- **Market impact.** A $2000 order would have moved the price and drawn a reaction. The book is
  treated as a passive object, so the ladder is OPTIMISTIC at the large end.
- **Depth beyond what was recorded.** Only the levels FRIDAY saw (5 a side by default) exist here,
  so the ladder is PESSIMISTIC in deep markets.

The two errors point in opposite directions and neither is small. The ladder is a sizing sanity
check, not a capacity guarantee: if it says the edge dies at $100, believe that; if it says the
edge survives to $2000, do not.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable

from .costs import CostSettings, cost_of
from .fills import buy_for_stake, capacity, sell_shares

CAPACITY_VERSION = "14B.1"
LADDER: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)


def _book(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def replay(entry_book: Any, exit_book: Any, stake: float, settings: CostSettings,
           category: str | None, max_slippage: float | None = 0.02,
           min_depth_usd: float = 5.0) -> dict[str, Any] | None:
    """Re-run one trade at `stake` against the books it really saw. None when the entry book could
    not have absorbed it at all — that is a trade which would not have happened at this size, not
    a trade which broke even."""
    entry, exit_ = _book(entry_book), _book(exit_book)
    if not entry or not exit_:
        return None
    asks, bids = entry.get("asks"), exit_.get("bids")
    if not asks or not bids:
        return None
    if capacity(asks, max_slippage) < min_depth_usd:
        return None
    buy = buy_for_stake(asks, stake, max_slippage)
    if buy.price is None or buy.shares <= 0:
        return None
    entry_cost = cost_of(settings, buy.shares, buy.price, category)
    sell = sell_shares(bids, buy.shares)
    if sell.price is None:
        return None
    exit_cost = cost_of(settings, sell.shares, sell.price, category)
    spent = buy.stake + entry_cost.total_usd
    received = sell.stake - exit_cost.total_usd
    return {"requested": round(float(stake), 6), "filled": buy.stake, "shares": buy.shares,
            "entry_price": buy.price, "exit_price": sell.price,
            "fees_usd": round(entry_cost.total_usd + exit_cost.total_usd, 6),
            "net_usd": round(received - spent, 6),
            "complete": bool(buy.complete and sell.complete)}


def _mean_ci(values: list[float]) -> tuple[float, float, float]:
    """Mean and a 95% normal interval. Not a t-interval: with the sample sizes that matter here the
    difference is smaller than the modelling error above it."""
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, mean, mean
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    half = 1.96 * math.sqrt(var / n)
    return mean, mean - half, mean + half


def report(rows: Iterable[dict[str, Any]], settings: CostSettings,
           ladder: Iterable[float] = LADDER, max_slippage: float | None = 0.02,
           min_depth_usd: float = 5.0) -> dict[str, Any]:
    """`rows` are closed paper trades joined to their plans: strategy, units, entry/exit fill
    prices and fees, fee_category, entry_book, exit_book."""
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_strategy.setdefault(str(r.get("strategy") or "?"), []).append(r)

    out: dict[str, Any] = {"version": CAPACITY_VERSION, "ladder": list(ladder), "strategies": {}}
    for strategy, trades in sorted(by_strategy.items()):
        realised_gross = realised_fees = 0.0
        for r in trades:
            units = float(r.get("units") or 0.0)
            ep, xp = r.get("entry_fill_price"), r.get("exit_fill_price")
            if units > 0 and isinstance(ep, (int, float)) and isinstance(xp, (int, float)):
                realised_gross += (float(xp) - float(ep)) * units
            realised_fees += float(r.get("entry_fee_usd") or 0.0) + float(r.get("exit_fee_usd") or 0.0)

        rungs = []
        for stake in ladder:
            nets, fillable, fees, filled = [], 0, 0.0, 0.0
            for r in trades:
                sim = replay(r.get("entry_book"), r.get("exit_book"), stake, settings,
                             r.get("fee_category"), max_slippage, min_depth_usd)
                if sim is None:
                    continue
                fillable += 1
                nets.append(sim["net_usd"])
                fees += sim["fees_usd"]
                filled += sim["filled"]
            if not nets:
                continue
            mean, lo, hi = _mean_ci(nets)
            total = sum(nets)
            # A rung asked for more than the book held fills only part of it. Scoring that rung on
            # its partial fill would read as "the edge survives at this size" when the size was
            # never available, so the shortfall is reported and disqualifies the rung.
            ratio = round(filled / (float(stake) * fillable), 4) if fillable and stake else 0.0
            rungs.append({"stake": round(float(stake), 2), "trades": fillable,
                          "filled_usd": round(filled, 4), "fill_ratio": ratio,
                          "net_usd": round(total, 4), "mean_net_usd": round(mean, 4),
                          "ci95_low": round(lo, 4), "ci95_high": round(hi, 4),
                          "fees_usd": round(fees, 4),
                          # Positive mean is not enough: an interval straddling zero means the
                          # sample cannot tell this apart from noise at this size.
                          "profitable": total > 0 and lo > 0 and ratio >= 0.99})

        profitable = [r["stake"] for r in rungs if r["profitable"]]
        out["strategies"][strategy] = {
            "trades": len(trades),
            "realised_gross_usd": round(realised_gross, 4),
            "realised_fees_usd": round(realised_fees, 4),
            "realised_net_usd": round(realised_gross - realised_fees, 4),
            "fee_share_of_gross": (round(realised_fees / abs(realised_gross), 4)
                                   if realised_gross else None),
            "rungs": rungs,
            "max_profitable_stake": max(profitable) if profitable else None,
            "replayable": sum(1 for r in trades if _book(r.get("entry_book")) and _book(r.get("exit_book"))),
        }
    return out
