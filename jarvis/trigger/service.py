"""TRIGGER service (Phase 13A): joins the feed to the paper engine and the market store.

- Universe: tokens the paper engine HOLDS or has resting orders on come first (priority), then the
  tokens of the market-store watchlist, up to JARVIS_TRIGGER_MAX_TOKENS.
- Every valid quote refreshes an in-memory cache the paper engine reads (fresher-only).
- A priority move runs `PaperEngine.tick` at once: stops, targets and resting-order fills happen in
  seconds, not at the next scan. `tick` runs under the governor for anything that opens a position,
  and exits never need the governor, so this keeps working while the platform is TERMINATED.
- A market_resolved event is recorded as a resolution so held positions settle at once.
- Entries are NOT created here. A watchlist move is counted and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .agent import TriggerAgent, TriggerConfig, TriggerSignal
from .quotes import LiveQuotes

log = logging.getLogger("jarvis.trigger")


@dataclass(frozen=True)
class TriggerSettings:
    enabled: bool = False
    max_tokens: int = 150
    watch_hours: float = 48.0
    min_tick_interval_s: float = 1.0
    config: TriggerConfig = TriggerConfig()

    @classmethod
    def from_environment(cls) -> "TriggerSettings":
        d, c = cls(), TriggerConfig()

        def num(name, default):
            raw = os.getenv(name, "").strip()
            return type(default)(float(raw)) if raw else default
        raw = os.getenv("JARVIS_TRIGGER", "").strip().lower()
        cfg = TriggerConfig(move_threshold=num("JARVIS_TRIGGER_MOVE", c.move_threshold),
                            cooldown_s=num("JARVIS_TRIGGER_COOLDOWN_S", c.cooldown_s),
                            priority_move_threshold=num("JARVIS_TRIGGER_PRIORITY_MOVE", c.priority_move_threshold),
                            priority_cooldown_s=num("JARVIS_TRIGGER_PRIORITY_COOLDOWN_S", c.priority_cooldown_s))
        max_tokens = num("JARVIS_TRIGGER_MAX_TOKENS", d.max_tokens)
        if not 10 <= max_tokens <= 1000:
            raise ValueError("JARVIS_TRIGGER_MAX_TOKENS must be between 10 and 1000")
        return cls(enabled=raw in ("1", "true", "yes", "on"), max_tokens=max_tokens, config=cfg,
                   watch_hours=num("JARVIS_MARKET_WATCHLIST_HOURS", d.watch_hours) or d.watch_hours)


class TriggerService:
    def __init__(self, store, paper_engine, settings: TriggerSettings | None = None, connect=None) -> None:
        self.store, self.engine = store, paper_engine
        self.settings = settings or TriggerSettings()
        self.quotes = LiveQuotes()
        self.agent = TriggerAgent(self._on_signal, self.universe, self.settings.config,
                                  priority=self._is_priority, on_quote=self.quotes.set,
                                  on_resolved=self._on_resolved, connect=connect)
        self._task: asyncio.Task | None = None
        self._prio: set[str] = set()
        self._prio_at = 0.0
        self._last_tick = 0.0
        self.ticks = 0
        self.watch_moves = 0
        self.last_tick: dict[str, Any] | None = None

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._task is None:
            self.engine.live_quotes = self.quotes.get
            self._task = asyncio.create_task(self.agent.run(), name="jarvis.trigger")

    async def stop(self) -> None:
        self.agent.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.engine.live_quotes == self.quotes.get:
            self.engine.live_quotes = None                         # back to recorded quotes only

    def status(self) -> dict[str, Any]:
        return {"enabled": True, **self.agent.snapshot(), "priority_tokens": len(self._prio),
                "cached_quotes": len(self.quotes), "paper_ticks": self.ticks, "watch_moves": self.watch_moves,
                "last_tick": self.last_tick, "max_tokens": self.settings.max_tokens}

    # --------------------------------------------------------------- universe

    def _priority_now(self) -> set[str]:
        try:
            return self.engine.live_tokens()
        except Exception:                                          # noqa: BLE001
            log.exception("could not read held tokens")
            return self._prio

    def _is_priority(self, token: str) -> bool:
        if time.monotonic() - self._prio_at > 5.0:                 # a cheap query, at most every 5 s
            self._prio, self._prio_at = self._priority_now(), time.monotonic()
        return token in self._prio

    async def universe(self) -> list[str]:
        def build() -> list[str]:
            prio = sorted(self._priority_now())
            self._prio, self._prio_at = set(prio), time.monotonic()
            ids = [w["market_id"] for w in self.store.watchlist(self.settings.watch_hours)]
            rest = [t for t in self.store.tokens_for_markets(ids) if t not in self._prio]
            return (prio + sorted(rest))[: max(self.settings.max_tokens, len(prio))]
        return await asyncio.to_thread(build)

    # --------------------------------------------------------------- handlers

    async def _on_signal(self, sig: TriggerSignal) -> None:
        if not sig.priority:
            self.watch_moves += 1
            return
        wait = self.settings.min_tick_interval_s - (time.monotonic() - self._last_tick)
        if wait > 0:
            await asyncio.sleep(wait)                              # signals meanwhile coalesce to the newest price
        self._last_tick = time.monotonic()
        self.last_tick = await asyncio.to_thread(self.engine.tick)
        self.ticks += 1

    async def _on_resolved(self, ev: dict[str, Any]) -> None:
        assets = [str(a) for a in ev.get("assets_ids") or []]
        winner = str(ev.get("winning_asset_id") or "")
        if not assets or winner not in assets:
            return

        def record() -> bool:
            market_id = self.store.market_for_token(assets[0])
            if market_id is None:
                return False
            self.store.record_resolution(market_id, {a: 1.0 if a == winner else 0.0 for a in assets}, "ws")
            return True
        recorded = await asyncio.to_thread(record)
        self.quotes.drop(assets)
        if recorded:
            self._last_tick = 0.0
            self.last_tick = await asyncio.to_thread(self.engine.tick)
            self.ticks += 1
