"""
Hermes controller — the single operational handle FRIDAY holds.

Phase 3C state machine:

    STOPPED --start--> RUNNING <--pause/resume--> PAUSED
       ^                  |                          |
       +------stop--------+-----------stop-----------+
    any --kill--> KILLED --reset (human)--> STOPPED

- RUNNING: scheduler submits due jobs; worker executes them
- PAUSED:  scheduler and worker halted; queue retained; manual
           submissions are queued but not executed until resume
- KILLED:  scheduler and worker halted, every queued job cancelled,
           all new submissions refused. Only an explicit reset()
           (a human decision) leaves KILLED.

The kill switch stops Hermes. It does not, and cannot, affect the
execution lock, which stays locked regardless of Hermes' state.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Callable

from friday.core.events import Event

from .jobs import Job, JobRequest
from .runtime import HermesRuntime
from .scheduler import HermesScheduler, Schedule


class HermesState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    KILLED = "KILLED"


class HermesControlError(RuntimeError):
    """Illegal control transition."""


class HermesController:
    def __init__(
        self,
        runtime: HermesRuntime,
        scheduler: HermesScheduler | None = None,
        on_state_change: Callable[[HermesState], Any] | None = None,
    ):
        self.runtime = runtime
        self.scheduler = scheduler or HermesScheduler(runtime)
        self._on_state_change = on_state_change or (lambda state: None)
        self._state = HermesState.STOPPED
        self._lock = threading.RLock()
        self._kill_reason: str | None = None

    # ------------------------------------------------------------ state

    @property
    def state(self) -> HermesState:
        with self._lock:
            return self._state

    def _set(self, new_state: HermesState, message: str, severity: str = "INFO", **extra) -> None:
        self._state = new_state
        self.runtime._emit(Event(
            kind="HERMES_CONTROL", message=message, source="HERMES",
            component="HERMES", severity=severity,
            payload={"state": new_state.value, "execution_authorized": False, **extra},
        ))
        try:
            self._on_state_change(new_state)
        except Exception:
            pass

    # ------------------------------------------------------------ transitions

    def start(self) -> None:
        with self._lock:
            if self._state is HermesState.KILLED:
                raise HermesControlError("Hermes is KILLED; a human reset() is required.")
            if self._state is HermesState.RUNNING:
                return
            if self._state is HermesState.PAUSED:
                raise HermesControlError("Hermes is PAUSED; use resume().")
            self.runtime.start()
            self.scheduler.start()
            self._set(HermesState.RUNNING, "Hermes started")

    def pause(self) -> None:
        with self._lock:
            if self._state is not HermesState.RUNNING:
                raise HermesControlError(f"Cannot pause from {self._state.value}")
            self.scheduler.stop()
            self.runtime.stop()
            self._set(HermesState.PAUSED, "Hermes paused")

    def resume(self) -> None:
        with self._lock:
            if self._state is not HermesState.PAUSED:
                raise HermesControlError(f"Cannot resume from {self._state.value}")
            self.runtime.start()
            self.scheduler.start()
            self._set(HermesState.RUNNING, "Hermes resumed")

    def stop(self) -> None:
        with self._lock:
            if self._state in (HermesState.STOPPED, HermesState.KILLED):
                return
            self.scheduler.stop()
            self.runtime.stop()
            self._set(HermesState.STOPPED, "Hermes stopped")

    def kill(self, reason: str = "Kill switch activated") -> int:
        """
        Emergency stop. Returns the number of queued jobs cancelled.
        Idempotent: killing a KILLED Hermes cancels any stragglers.
        """

        with self._lock:
            # Close intake FIRST so no in-flight scheduler tick or caller
            # can queue work after the kill.
            self.runtime.close_intake(f"Hermes is KILLED: {reason}"[:500])
            self.scheduler.stop()
            self.runtime.stop()
            cancelled = self.runtime.cancel_all_pending(reason=f"KILLED: {reason}")
            self._kill_reason = reason[:500]
            self._set(HermesState.KILLED, "Hermes KILL SWITCH activated", severity="CRITICAL",
                      reason=self._kill_reason, cancelled_jobs=cancelled)
            return cancelled

    def reset(self, confirmed_by: str) -> None:
        """
        Leave KILLED. Requires a named human confirmation; Hermes returns
        to STOPPED and must be started explicitly.
        """

        if not confirmed_by or not str(confirmed_by).strip():
            raise HermesControlError("reset() requires confirmed_by (a human operator).")
        with self._lock:
            if self._state is not HermesState.KILLED:
                raise HermesControlError(f"reset() only applies to KILLED, not {self._state.value}")
            self._kill_reason = None
            self.runtime.open_intake()
            self._set(HermesState.STOPPED, "Hermes reset after kill",
                      confirmed_by=str(confirmed_by)[:64])

    # ------------------------------------------------------------ work intake

    def submit(self, request: JobRequest | dict[str, Any]) -> Job:
        # The runtime's intake gate enforces KILLED atomically.
        return self.runtime.submit(request)

    def add_schedule(self, definition: Schedule | dict[str, Any], run_immediately: bool = True) -> Schedule:
        return self.scheduler.add(definition, run_immediately=run_immediately)

    # ------------------------------------------------------------ reporting

    def component_status(self) -> str:
        """Value for FRIDAY's component table (see core/health.py)."""
        return self.state.value

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "kill_reason": self._kill_reason,
            "runtime": self.runtime.status(),
            "schedules": self.scheduler.list(),
            "execution_authorized": False,
        }
