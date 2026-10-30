"""ARBITER — the loss tracker.

Maintains a running tally of consecutive wins and losses.
Persists streak state so it survives restarts.
Triggers TERMINATOR when survival rules are breached.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

ARBITER_PATH = Path.home() / "jarvis" / "data" / "arbiter_state.json"

DEFAULT_STATE: dict[str, Any] = {
    "consecutive_losses": 0,
    "consecutive_wins": 0,
    "total_wins": 0,
    "total_losses": 0,
    "last_trade_at": None,
    "soft_pause_until": None,
}


class Arbiter:
    """Tracks trade streaks. A single source of truth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = dict(DEFAULT_STATE)
        self._load()

    def _load(self) -> None:
        if ARBITER_PATH.exists():
            try:
                with open(ARBITER_PATH) as f:
                    loaded = json.load(f)
                    self._state.update(loaded)
            except Exception:
                pass

    def _save(self) -> None:
        ARBITER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ARBITER_PATH, "w") as f:
            json.dump(self._state, f, indent=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def record_win(self) -> dict[str, Any]:
        import datetime
        with self._lock:
            self._state["consecutive_wins"] += 1
            self._state["consecutive_losses"] = 0
            self._state["total_wins"] += 1
            self._state["last_trade_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self._save()
            return dict(self._state)

    def record_loss(self) -> dict[str, Any]:
        import datetime
        with self._lock:
            self._state["consecutive_losses"] += 1
            self._state["consecutive_wins"] = 0
            self._state["total_losses"] += 1
            self._state["last_trade_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self._save()
            return dict(self._state)

    def should_terminate(self, max_losses: int) -> bool:
        with self._lock:
            return self._state["consecutive_losses"] >= max_losses

    def should_soft_pause(self, threshold: int) -> bool:
        with self._lock:
            return self._state["consecutive_losses"] >= threshold

    def reset_streak(self) -> None:
        with self._lock:
            self._state["consecutive_losses"] = 0
            self._state["consecutive_wins"] = 0
            self._save()


# ---- Singleton ----

_arbiter: Arbiter | None = None


def get_arbiter() -> Arbiter:
    global _arbiter
    if _arbiter is None:
        _arbiter = Arbiter()
    return _arbiter
