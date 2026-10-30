"""TRIGGER — the 24/7 heartbeat of the autonomous platform.

Runs an async loop that:
  1. Every `interval_seconds`, loads the current market snapshot.
  2. For each market, asks the registered strategies for signals.
  3. Fires a `SIGNAL_FOUND` event with the signal.
  4. The EXECUTOR listens and evaluates whether to trade.

This replaces the old 15-minute Hermes cron with a tighter,
always-on loop (default 60 seconds).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_INTERVAL = 60  # seconds


@dataclass
class TriggerEvent:
    at: str
    market_id: str
    strategy: str
    direction: str  # "UP", "DOWN", "WATCH"
    price: float
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class Trigger:
    """The autonomous event loop.

    `scan_fn` should return a list of dicts, each with at least:
        {"market_id": ..., "price": ..., "signals": ["RSI:UP", ...]}
    The Trigger fires one TriggerEvent per (market, signal) pair.
    """

    def __init__(
        self,
        scan_fn: Callable[[], list[dict[str, Any]]],
        event_sink: Callable[[TriggerEvent], None] | None = None,
        interval_seconds: int = DEFAULT_INTERVAL,
    ) -> None:
        self._scan_fn = scan_fn
        self._event_sink = event_sink or (lambda ev: None)
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats = {
            "cycles": 0,
            "events_fired": 0,
            "last_cycle_at": None,
            "last_error": None,
        }

    # ---- Lifecycle ----

    def start_background(self) -> None:
        """Start the trigger in a background thread with its own event loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="jarvis-trigger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    # ---- Internals ----

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._cycle())
        finally:
            self._loop.close()

    async def _cycle(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self._scan_fn() or []
                fired = 0
                for row in rows:
                    market_id = row.get("market_id", "?")
                    price = float(row.get("price", 0.0))
                    for sig in row.get("signals", []) or []:
                        # sig format: "RSI:UP" or "Bollinger:DOWN"
                        parts = str(sig).split(":", 1)
                        strategy = parts[0]
                        direction = parts[1] if len(parts) > 1 else "WATCH"
                        ev = TriggerEvent(
                            at=datetime.now(timezone.utc).isoformat(),
                            market_id=market_id,
                            strategy=strategy,
                            direction=direction,
                            price=price,
                            payload=row,
                        )
                        try:
                            self._event_sink(ev)
                            fired += 1
                        except Exception as e:
                            self._stats["last_error"] = f"{type(e).__name__}: {e}"
                self._stats["cycles"] += 1
                self._stats["events_fired"] += fired
                self._stats["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                self._stats["last_error"] = f"{type(e).__name__}: {e}"

            await asyncio.sleep(self._interval)


# ---- Singleton ----

_trigger: Trigger | None = None


def get_trigger() -> Trigger | None:
    return _trigger


def init_trigger(
    scan_fn: Callable[[], list[dict[str, Any]]],
    event_sink: Callable[[TriggerEvent], None] | None = None,
    interval_seconds: int = DEFAULT_INTERVAL,
) -> Trigger:
    global _trigger
    _trigger = Trigger(scan_fn, event_sink, interval_seconds)
    return _trigger
