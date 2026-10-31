"""
FRIDAY fill model (Phase 14A): what a given size would actually have cost.

Until 14A the paper engine filled any size at the best price on the book. Ask for $500 where $40
rests at the best ask and it handed you $500 at that price. That is free money which does not
exist, and it grows with position size — so a profitable paper record was not evidence that the
strategy worked, only that the fill model was generous.

This walks the order book FRIDAY actually recorded, level by level, the way a real taker does.

Prices are $ per $1-payoff share; `size` on a level is a number of shares, so the money resting at
a level is `price * size`.

Levels arrive BEST FIRST (bids descending, asks ascending) — `Book` in jarvis.market.models sorts
them that way before they are stored, and `MarketStore.levels` returns them in that order.

What this model still assumes, and therefore what a paper record still cannot prove:
  - The whole visible book is available to us. In reality part of it may be cancelled in the
    moments it takes to act.
  - We are alone. Our own order does not move the price and nobody reacts to it.
  - Only the levels FRIDAY recorded exist (5 a side by default). Deeper liquidity is invisible, so
    a large order is modelled PESSIMISTICALLY, which is the safe direction.
  - A snapshot is up to 15 minutes old. The book has moved since.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

FILLS_VERSION = "14A.1"


@dataclass(frozen=True)
class Fill:
    """The result of walking a book. `price` is the volume-weighted average actually paid/received."""
    price: float | None           # None: nothing could be filled
    stake: float                  # USD actually committed (buy) or received (sell)
    shares: float
    levels_used: int
    top_price: float | None       # the best price on the book — what the old model assumed
    complete: bool                # the full request was filled

    @property
    def slippage(self) -> float | None:
        """How much worse than the top of book, per share. Always >= 0 by construction."""
        if self.price is None or self.top_price is None:
            return None
        return round(abs(self.price - self.top_price), 6)

    def as_dict(self) -> dict[str, Any]:
        return {"price": self.price, "stake": round(self.stake, 6), "shares": round(self.shares, 6),
                "levels_used": self.levels_used, "top_price": self.top_price,
                "slippage": self.slippage, "complete": self.complete}


EMPTY = Fill(price=None, stake=0.0, shares=0.0, levels_used=0, top_price=None, complete=False)


def _clean(levels: Iterable[Any]) -> list[tuple[float, float]]:
    """Keep only usable (price, size) pairs, in the order given (best first)."""
    out: list[tuple[float, float]] = []
    for level in levels or ():
        if isinstance(level, dict):
            price, size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            continue
        try:
            price, size = float(price), float(size)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and math.isfinite(size) and 0 < price <= 1 and size > 0:
            out.append((price, size))
    return out


def _limit_ok(price: float, top: float, max_slippage: float | None, buying: bool) -> bool:
    if max_slippage is None:
        return True
    return price <= top + max_slippage + 1e-9 if buying else price >= top - max_slippage - 1e-9


def buy_for_stake(levels: Iterable[Any], stake: float, max_slippage: float | None = None) -> Fill:
    """Spend up to `stake` USD taking asks, best price first.

    `max_slippage` is how far past the best ask we are willing to pay; levels beyond it are left
    alone, which is what a limit order would do. The result may therefore be a partial fill."""
    book = _clean(levels)
    if not book or not (isinstance(stake, (int, float)) and math.isfinite(stake) and stake > 0):
        return EMPTY
    top = book[0][0]
    spent = shares = 0.0
    used = 0
    for price, size in book:
        if not _limit_ok(price, top, max_slippage, buying=True):
            break
        room = stake - spent
        if room <= 1e-9:
            break
        take_usd = min(room, price * size)
        spent += take_usd
        shares += take_usd / price
        used += 1
    if spent <= 0 or shares <= 0:
        return Fill(None, 0.0, 0.0, 0, top, False)
    return Fill(price=round(spent / shares, 6), stake=round(spent, 6), shares=round(shares, 6),
                levels_used=used, top_price=top, complete=spent >= stake - 1e-6)


def sell_shares(levels: Iterable[Any], shares: float, max_slippage: float | None = None) -> Fill:
    """Sell up to `shares` into bids, best price first. Used for exits, where the quantity is
    shares already held rather than a sum of money."""
    book = _clean(levels)
    if not book or not (isinstance(shares, (int, float)) and math.isfinite(shares) and shares > 0):
        return EMPTY
    top = book[0][0]
    sold = proceeds = 0.0
    used = 0
    for price, size in book:
        if not _limit_ok(price, top, max_slippage, buying=False):
            break
        room = shares - sold
        if room <= 1e-9:
            break
        take = min(room, size)
        sold += take
        proceeds += take * price
        used += 1
    if sold <= 0:
        return Fill(None, 0.0, 0.0, 0, top, False)
    return Fill(price=round(proceeds / sold, 6), stake=round(proceeds, 6), shares=round(sold, 6),
                levels_used=used, top_price=top, complete=sold >= shares - 1e-6)


def capacity(levels: Iterable[Any], max_slippage: float | None = None) -> float:
    """The most that could be bought within the slippage band, in USD. This is the honest ceiling
    on a position: ask for more and the extra is imaginary."""
    book = _clean(levels)
    if not book:
        return 0.0
    top = book[0][0]
    return round(sum(price * size for price, size in book
                     if _limit_ok(price, top, max_slippage, buying=True)), 6)
