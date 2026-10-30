"""BROKER — abstract trading broker interface.

A broker places orders. Today we have `PaperBroker` (simulated).
Tomorrow, `PolymarketBroker` will wrap py-clob-client.

Every broker method is async so the live Polymarket broker can be
implemented without changing the calling code.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class OrderRequest:
    market_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "Yes" or "No"
    limit_price: float
    stake: float  # USD


@dataclass
class OrderResult:
    order_id: str
    status: str  # "FILLED", "RESTING", "REJECTED"
    filled_price: float | None
    filled_stake: float | None
    reason: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Broker:
    """Abstract base. Subclasses implement these 3 methods."""

    async def place_order(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    async def get_order_status(self, order_id: str) -> OrderResult:
        raise NotImplementedError


class PaperBroker(Broker):
    """Simulated broker. Fills immediately at the limit price.

    Real Polymarket uses order books and can partially fill. This
    PaperBroker is intentionally simple: if the stake is valid, it
    fills at the limit price.
    """

    def __init__(self) -> None:
        self._orders: dict[str, OrderResult] = {}

    async def place_order(self, req: OrderRequest) -> OrderResult:
        await asyncio.sleep(0)  # yield to event loop

        if req.stake <= 0 or req.limit_price <= 0:
            result = OrderResult(
                order_id=str(uuid.uuid4()),
                status="REJECTED",
                filled_price=None,
                filled_stake=None,
                reason="invalid stake or price",
            )
            self._orders[result.order_id] = result
            return result

        result = OrderResult(
            order_id=str(uuid.uuid4()),
            status="FILLED",
            filled_price=req.limit_price,
            filled_stake=req.stake,
            reason="paper fill at limit",
        )
        self._orders[result.order_id] = result
        return result

    async def cancel_order(self, order_id: str) -> bool:
        await asyncio.sleep(0)
        if order_id in self._orders and self._orders[order_id].status == "RESTING":
            self._orders[order_id].status = "CANCELLED"
            return True
        return False

    async def get_order_status(self, order_id: str) -> OrderResult:
        await asyncio.sleep(0)
        return self._orders.get(
            order_id,
            OrderResult(
                order_id=order_id,
                status="UNKNOWN",
                filled_price=None,
                filled_stake=None,
                reason="order not found",
            ),
        )
