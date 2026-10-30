"""SETTLEMENT — pre-resolution exit logic for binary markets.

On Polymarket, a market resolves at $1.00 or $0.00. If you hold a
winning token into resolution, you get $1.00 per share. If you hold
a losing token, you get $0.00.

This agent:
  1. Every N seconds, scans open positions.
  2. For each position approaching resolution (within X minutes),
     decides: hold to settlement OR exit on the order book.
  3. If exit is better (e.g., price is near $0.95 and holding is
     risky due to volatility), it asks the EXECUTOR to sell.

Today it only logs decisions. When the LiveBroker lands, it will
actually place sell orders.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class SettlementDecision:
    at: str
    market_id: str
    token_id: str
    label: str
    current_price: float
    hours_to_resolution: float
    decision: str  # "HOLD", "SELL", "URGENT_SELL"
    reason: str


class Settlement:
    """Monitors open positions and recommends exits near resolution."""

    def __init__(
        self,
        positions_getter: Callable[[], list[dict[str, Any]]],
        sell_callback: Callable[[str, str, float, float], None] | None = None,
        interval_seconds: int = 300,
        urgent_hours: float = 1.0,
        warn_hours: float = 6.0,
    ) -> None:
        self._positions_getter = positions_getter
        self._sell_callback = sell_callback or (lambda mid, tid, price, stake: None)
        self._interval = interval_seconds
        self._urgent_hours = urgent_hours
        self._warn_hours = warn_hours
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_decisions: list[SettlementDecision] = []

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="jarvis-settlement", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def decisions(self) -> list[SettlementDecision]:
        return list(self._last_decisions)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:
                pass
            time.sleep(self._interval)

    def _cycle(self) -> None:
        positions = self._positions_getter() or []
        decisions: list[SettlementDecision] = []

        for pos in positions:
            # Assume the position dict has:
            #   market_id, token_id, label, current_price, end_time (ISO)
            end_time = pos.get("end_time")
            if not end_time:
                continue
            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            except Exception:
                continue

            hours_to = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
            price = float(pos.get("current_price", 0.0))

            if hours_to < 0:
                continue  # already resolved

            if hours_to <= self._urgent_hours:
                decision = "URGENT_SELL" if price >= 0.5 else "HOLD"
                reason = f"resolution in {hours_to:.1f}h at price {price:.2f}"
            elif hours_to <= self._warn_hours:
                decision = "SELL" if price >= 0.8 else "HOLD"
                reason = f"resolution in {hours_to:.1f}h, price near ceiling"
            else:
                decision = "HOLD"
                reason = f"resolution in {hours_to:.1f}h, no action yet"

            d = SettlementDecision(
                at=datetime.now(timezone.utc).isoformat(),
                market_id=str(pos.get("market_id", "?")),
                token_id=str(pos.get("token_id", "?")),
                label=str(pos.get("label", "")),
                current_price=price,
                hours_to_resolution=hours_to,
                decision=decision,
                reason=reason,
            )
            decisions.append(d)

            if decision in ("SELL", "URGENT_SELL"):
                try:
                    self._sell_callback(d.market_id, d.token_id, price, float(pos.get("stake", 0.0)))
                except Exception:
                    pass

        self._last_decisions = decisions


# ---- Singleton ----

_settlement: Settlement | None = None


def get_settlement() -> Settlement | None:
    return _settlement


def init_settlement(positions_getter, sell_callback=None, interval_seconds=300) -> Settlement:
    global _settlement
    _settlement = Settlement(positions_getter, sell_callback, interval_seconds)
    return _settlemento

