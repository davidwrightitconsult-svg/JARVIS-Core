"""
TRIGGER (Phase 13A): the real-time market watcher. PAPER only, read-only.

Listens to Polymarket's public CLOB market channel and tells the rest of FRIDAY the moment a
price it cares about moves, instead of waiting for the next 15-minute scan.

What it may do
--------------
- keep an in-memory top-of-book for every watched token (`on_quote`),
- wake a handler when a price moves enough (`on_signal`),
- report a market resolution (`on_resolved`).

What it may not do
------------------
It has no way to size, approve or place anything. It never imports the Risk Governor. Whatever
`on_signal` does goes through the same governed paths as everything else.

Rules of the design
-------------------
1. The socket reader never blocks. The handler runs in its own worker, so a slow handler can not
   starve the heartbeat or back up the read buffer.
2. Signals are coalesced per token ("latest wins"). 40 ticks while the handler is busy become one
   call on the newest price, never a backlog of stale ones.
3. Tokens the engine HOLDS (priority) use a tighter threshold and a short cooldown: a 3-cent stop
   must not wait for a 2-cent move on top of a 30-second cooldown.
4. Silence is a failure: no frame for `stale_after_s` forces a reconnect.
5. The first quote of a token is a baseline. Only priority tokens signal on it (one look at a
   position right after connecting), because a position may already be through its stop.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market (public, no auth). This is the
venue py-clob-client trades on. It is NOT api.polymarket.us, which is a different exchange.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger("jarvis.trigger")

TRIGGER_VERSION = "13A.1"
CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class TriggerConfig:
    ws_url: str = CLOB_MARKET_WS
    move_threshold: float = 0.02           # watchlist: mid moved this much since the last evaluation
    cooldown_s: float = 30.0
    priority_move_threshold: float = 0.005  # held positions / resting orders
    priority_cooldown_s: float = 2.0
    max_spread: float = 0.10               # watchlist only: a hollow book has no meaningful mid
    ping_interval_s: float = 10.0          # Polymarket requires a PING text frame every 10 s
    stale_after_s: float = 60.0
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    universe_refresh_s: float = 60.0
    custom_features: bool = True           # adds best_bid_ask and market_resolved events

    def __post_init__(self):
        if not (0 < self.priority_move_threshold <= self.move_threshold <= 0.5):
            raise ValueError("thresholds must satisfy 0 < priority <= normal <= 0.5")
        if not (0 <= self.priority_cooldown_s <= self.cooldown_s <= 3600):
            raise ValueError("cooldowns must satisfy 0 <= priority <= normal <= 3600")
        if not (0 < self.max_spread <= 0.5):
            raise ValueError("max_spread must be in (0, 0.5]")
        if not (0 < self.ping_interval_s <= 10) or self.stale_after_s <= self.ping_interval_s:
            raise ValueError("ping interval must be in (0, 10] s (Polymarket drops idle sockets) and below stale_after_s")
        if not self.ws_url.startswith("wss://"):
            raise ValueError("the market feed must be wss://")


@dataclass(frozen=True)
class TriggerSignal:
    token_id: str
    market: str                 # the CLOB condition id carried by the feed
    mid: float
    prev_mid: float | None
    best_bid: float
    best_ask: float
    reason: str                 # "price_move" | "baseline"
    priority: bool
    ts: float = field(default_factory=time.time)

    @property
    def delta(self) -> float:
        return 0.0 if self.prev_mid is None else self.mid - self.prev_mid


SignalHandler = Callable[[TriggerSignal], Awaitable[None]]
AssetProvider = Callable[[], Awaitable[Iterable[str]]]
QuoteHook = Callable[[str, str, float, float, float], None]   # token, market, bid, ask, epoch seconds
ResolvedHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _Asset:
    market: str = ""
    bid: float | None = None
    ask: float | None = None
    eval_mid: float | None = None
    eval_at: float = 0.0


@dataclass
class TriggerStats:
    connected: bool = False
    connects: int = 0
    frames: int = 0
    quotes: int = 0
    signals: int = 0
    coalesced: int = 0
    handler_errors: int = 0
    resolutions: int = 0
    watching: int = 0
    last_frame_age_s: float | None = None
    last_error: str | None = None


class TriggerAgent:
    def __init__(self, on_signal: SignalHandler, asset_provider: AssetProvider,
                 config: TriggerConfig | None = None, *,
                 priority: Callable[[str], bool] | None = None,
                 on_quote: QuoteHook | None = None,
                 on_resolved: ResolvedHook | None = None,
                 connect: Callable[..., Any] | None = None) -> None:
        self.cfg = config or TriggerConfig()
        self.on_signal, self.asset_provider = on_signal, asset_provider
        self.priority = priority or (lambda _t: False)
        self.on_quote, self.on_resolved = on_quote, on_resolved
        self._connect = connect or _default_connect
        self.stats = TriggerStats()
        self._assets: dict[str, _Asset] = {}
        self._watching: set[str] = set()
        self._pending: dict[str, TriggerSignal] = {}
        self._pending_evt = asyncio.Event()
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._last_frame = time.monotonic()

    # ------------------------------------------------------------------ public

    async def run(self) -> None:
        worker = asyncio.create_task(self._worker(), name="trigger.worker")
        backoff = self.cfg.backoff_initial_s
        try:
            while not self._stop.is_set():
                try:
                    if await self._session():
                        backoff = self.cfg.backoff_initial_s      # a session that connected: start over
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                          # noqa: BLE001
                    self.stats.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                    log.warning("trigger session ended: %s", self.stats.last_error)
                finally:
                    self.stats.connected = False
                    self._ws = None
                if self._stop.is_set():
                    break
                await self._sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, self.cfg.backoff_max_s)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        self._pending_evt.set()

    def snapshot(self) -> dict[str, Any]:
        s = self.stats
        age = None if not s.connected else round(time.monotonic() - self._last_frame, 1)
        return {"version": TRIGGER_VERSION, "connected": s.connected, "connects": s.connects, "watching": s.watching,
                "frames": s.frames, "quotes": s.quotes, "signals": s.signals, "coalesced": s.coalesced,
                "handler_errors": s.handler_errors, "resolutions": s.resolutions, "last_frame_age_s": age,
                "last_error": s.last_error, "endpoint": self.cfg.ws_url}

    # ----------------------------------------------------------------- session

    async def _session(self) -> bool:
        tokens = {str(t) for t in await self.asset_provider() if t}
        if not tokens:
            self.stats.last_error = "nothing to watch"
            await self._sleep(self.cfg.universe_refresh_s)
            return False
        async with self._connect(self.cfg.ws_url) as ws:
            self._ws = ws
            self._last_frame = time.monotonic()
            self._assets.clear()
            self._pending.clear()
            await self._send({"type": "market", "assets_ids": sorted(tokens),
                              "custom_feature_enabled": self.cfg.custom_features})
            self._watching = tokens
            self.stats.connected, self.stats.watching = True, len(tokens)
            self.stats.connects += 1
            log.info("trigger connected; watching %d tokens", len(tokens))
            tasks = [asyncio.create_task(c, name=n) for n, c in (
                ("trigger.reader", self._reader(ws)), ("trigger.ping", self._heartbeat()),
                ("trigger.watchdog", self._watchdog()), ("trigger.universe", self._universe()),
                ("trigger.stop", self._stop.wait()))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.cancelled() and t.exception():
                        raise t.exception()                      # type: ignore[misc]
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return True

    async def _reader(self, ws: Any) -> None:
        async for raw in ws:
            self.stats.frames += 1
            self._last_frame = time.monotonic()
            if raw == "PONG":
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for ev in payload if isinstance(payload, list) else [payload]:   # the first frame is an array
                if isinstance(ev, dict):
                    await self.handle_event(ev)
        raise ConnectionError("socket closed by the server")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.ping_interval_s)
            await self._ws.send("PING")

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(5.0, self.cfg.stale_after_s / 2))
            idle = time.monotonic() - self._last_frame
            if idle > self.cfg.stale_after_s:
                raise TimeoutError(f"no frames for {idle:.0f}s")

    async def _universe(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.universe_refresh_s)
            new = {str(t) for t in await self.asset_provider() if t}
            add, drop = new - self._watching, self._watching - new
            if add:
                await self._send({"assets_ids": sorted(add), "operation": "subscribe"})
            if drop:
                await self._send({"assets_ids": sorted(drop), "operation": "unsubscribe"})
                for t in drop:
                    self._assets.pop(t, None)
                    self._pending.pop(t, None)
            self._watching = new
            self.stats.watching = len(new)

    # ------------------------------------------------------------------ events

    async def handle_event(self, ev: dict[str, Any]) -> None:
        """Public so it can be tested without a socket."""
        kind, market = ev.get("event_type"), ev.get("market", "")
        if kind == "book":
            bids, asks = ev.get("bids") or [], ev.get("asks") or []
            self._update(ev.get("asset_id", ""), market,
                         max((_f(b.get("price")) or 0.0 for b in bids), default=None) or None,
                         min((p for p in (_f(a.get("price")) for a in asks) if p), default=None))
        elif kind == "price_change":
            for pc in ev.get("price_changes") or []:
                self._update(pc.get("asset_id", ""), market, _f(pc.get("best_bid")), _f(pc.get("best_ask")))
        elif kind == "best_bid_ask":
            self._update(ev.get("asset_id", ""), market, _f(ev.get("best_bid")), _f(ev.get("best_ask")))
        elif kind == "market_resolved":
            self.stats.resolutions += 1
            for t in ev.get("assets_ids") or []:
                self._assets.pop(t, None)
                self._pending.pop(t, None)
            if self.on_resolved:
                try:
                    await self.on_resolved(ev)
                except Exception:                                  # noqa: BLE001
                    log.exception("resolution hook failed")

    def _update(self, token: str, market: str, bid: float | None, ask: float | None) -> None:
        if not token or token not in self._watching:
            return
        a = self._assets.setdefault(token, _Asset())
        a.market = market or a.market
        if bid is not None and bid > 0:
            a.bid = bid
        if ask is not None and ask > 0:
            a.ask = ask
        if a.bid is None or a.ask is None or a.bid > a.ask:
            return
        self.stats.quotes += 1
        if self.on_quote:
            try:
                self.on_quote(token, a.market, a.bid, a.ask, time.time())
            except Exception:                                      # noqa: BLE001
                log.exception("quote hook failed")
        prio = bool(self.priority(token))
        if not prio and a.ask - a.bid > self.cfg.max_spread:
            return
        mid, now = round((a.bid + a.ask) / 2, 4), time.monotonic()
        if a.eval_mid is None:
            a.eval_mid, a.eval_at = mid, now
            if not prio:
                return                                            # a baseline is never a trade reason
            reason = "baseline"
        else:
            move = self.cfg.priority_move_threshold if prio else self.cfg.move_threshold
            cool = self.cfg.priority_cooldown_s if prio else self.cfg.cooldown_s
            if abs(mid - a.eval_mid) < move or now - a.eval_at < cool:
                return
            reason = "price_move"
        sig = TriggerSignal(token, a.market, mid, None if reason == "baseline" else a.eval_mid,
                            a.bid, a.ask, reason, prio)
        a.eval_mid, a.eval_at = mid, now
        if token in self._pending:
            self.stats.coalesced += 1
        self._pending[token] = sig
        self._pending_evt.set()

    async def _worker(self) -> None:
        while not self._stop.is_set():
            await self._pending_evt.wait()
            self._pending_evt.clear()
            while self._pending and not self._stop.is_set():
                token = next(iter(self._pending))
                sig = self._pending.pop(token)
                self.stats.signals += 1
                try:
                    await self.on_signal(sig)
                except asyncio.CancelledError:
                    raise
                except Exception:                                  # noqa: BLE001
                    self.stats.handler_errors += 1
                    log.exception("signal handler failed for %s", token[:12])

    # ------------------------------------------------------------------- utils

    async def _send(self, obj: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(obj))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _default_connect(url: str):
    from websockets.asyncio.client import connect                  # websockets >= 13
    return connect(url, ping_interval=None, max_size=2 ** 22, open_timeout=15)
