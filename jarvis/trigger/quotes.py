"""Live quote cache (Phase 13A): the newest top-of-book TRIGGER has seen, in memory only.

PaperEngine reads it through `live_quotes`; it only ever wins over a recorded quote by being
fresher, and it ages out on its own, so a dead socket falls back to the recorded quotes."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

MAX_TOKENS = 5000


class LiveQuotes:
    def __init__(self) -> None:
        self._q: dict[str, tuple[float, float, float, dict | None]] = {}   # token -> (bid, ask, epoch, book)
        self._lock = threading.Lock()

    def set(self, token: str, _market: str, bid: float, ask: float, ts: float,
            book: dict | None = None) -> None:
        with self._lock:
            self._q[token] = (bid, ask, ts, book)
            if len(self._q) > MAX_TOKENS:                         # drop the oldest quarter
                for t, _ in sorted(self._q.items(), key=lambda kv: kv[1][2])[:MAX_TOKENS // 4]:
                    self._q.pop(t, None)

    def get(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            v = self._q.get(token)
        if v is None:
            return None
        return {"bid": v[0], "ask": v[1],
                "at": datetime.fromtimestamp(v[2], timezone.utc).isoformat(timespec="seconds"),
                # 14A: the real book when the feed last sent a full snapshot. None after a bare
                # price_change, and the engine then falls back to the recorded book.
                "levels": v[3]}

    def drop(self, tokens) -> None:
        with self._lock:
            for t in tokens:
                self._q.pop(t, None)

    def __len__(self) -> int:
        return len(self._q)
