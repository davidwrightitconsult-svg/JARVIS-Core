"""TERMINATOR — the kill switch.

Executes the shutdown protocol when ARBITER confirms that survival
directives have been breached.

The protocol is deliberately multi-stage:
  1. Cancel all open orders.
  2. Set a hard `TERMINATED` flag.
  3. Emit a global event (Alfred speaks it).
  4. The platform refuses all further trades.

The shutdown is reversible by voice ("Alfred, resume trading")
but requires explicit confirmation.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path
import json
from typing import Any

TERMINATOR_STATE_PATH = Path.home() / "jarvis" / "data" / "terminator_state.json"

DEFAULT_STATE: dict[str, Any] = {
    "terminated": False,
    "terminated_at": None,
    "reason": None,
    "last_termination_by": None,
}


class Terminator:
    """The kill-switch authority. Refuses all trades when active."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = dict(DEFAULT_STATE)
        self._load()

    def _load(self) -> None:
        if TERMINATOR_STATE_PATH.exists():
            try:
                with open(TERMINATOR_STATE_PATH) as f:
                    loaded = json.load(f)
                    self._state.update(loaded)
            except Exception:
                pass

    def _save(self) -> None:
        TERMINATOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TERMINATOR_STATE_PATH, "w") as f:
            json.dump(self._state, f, indent=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def is_terminated(self) -> bool:
        with self._lock:
            return bool(self._state["terminated"])

    def terminate(self, reason: str, by: str = "system") -> None:
        with self._lock:
            self._state["terminated"] = True
            self._state["terminated_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            self._state["reason"] = reason
            self._state["last_termination_by"] = by
            self._save()

    def resume(self, by: str = "user") -> None:
        """Resume trading after a termination (requires user confirmation)."""
        with self._lock:
            self._state["terminated"] = False
            self._state["reason"] = f"Resumed by {by} at {datetime.datetime.now(datetime.timezone.utc).isoformat()}"
            self._save()


# ---- Singleton ----

_terminator: Terminator | None = None


def get_terminator() -> Terminator:
    global _terminator
    if _terminator is None:
        _terminator = Terminator()
    return _terminator
