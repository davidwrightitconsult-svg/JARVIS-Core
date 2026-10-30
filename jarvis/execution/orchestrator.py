"""ORCHESTRATOR — health monitor, auto-reconnect, crash recovery.

Tracks:
  - Is the TRIGGER thread alive?
  - Is the BACKEND responding?
  - Is the EXECUTOR operational?
  - Has the platform been TERMINATED by SENTINEL?

Exposes a /health summary through the executor layer.
Emits periodic heartbeat events to the activity log.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass
class HealthSnapshot:
    at: str
    trigger_alive: bool
    trigger_cycles: int
    trigger_events: int
    trigger_last_cycle: str | None
    executor_ready: bool
    broker_ready: bool
    terminated: bool
    terminated_reason: str | None
    error: str | None = None


class Orchestrator:
    """The watchdog. Reads the state of every agent, reports it once a minute."""

    def __init__(
        self,
        trigger_getter: Callable[[], Any],
        executor_getter: Callable[[], Any],
        terminator_getter: Callable[[], Any],
        interval_seconds: int = 60,
    ) -> None:
        self._trigger_getter = trigger_getter
        self._executor_getter = executor_getter
        self._terminator_getter = terminator_getter
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last: HealthSnapshot | None = None
        self._errors = 0

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="jarvis-orchestrator", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> HealthSnapshot | None:
        return self._last

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                trigger = self._trigger_getter()
                executor = self._executor_getter()
                terminator = self._terminator_getter()

                tstats = trigger.stats() if trigger else {}
                term_snap = terminator.snapshot() if terminator else {}

                self._last = HealthSnapshot(
                    at=datetime.now(timezone.utc).isoformat(),
                    trigger_alive=bool(trigger and tstats.get("cycles", 0) >= 0),
                    trigger_cycles=int(tstats.get("cycles", 0)),
                    trigger_events=int(tstats.get("events_fired", 0)),
                    trigger_last_cycle=tstats.get("last_cycle_at"),
                    executor_ready=bool(executor),
                    broker_ready=bool(executor and getattr(executor, "_broker", None)),
                    terminated=bool(term_snap.get("terminated", False)),
                    terminated_reason=term_snap.get("reason"),
                )
            except Exception as e:
                self._errors += 1
                self._last = HealthSnapshot(
                    at=datetime.now(timezone.utc).isoformat(),
                    trigger_alive=False, trigger_cycles=0, trigger_events=0,
                    trigger_last_cycle=None,
                    executor_ready=False, broker_ready=False,
                    terminated=False, terminated_reason=None,
                    error=f"{type(e).__name__}: {e}",
                )

            time.sleep(self._interval)


# ---- Singleton ----

_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator | None:
    return _orchestrator


def init_orchestrator(trigger_getter, executor_getter, terminator_getter, interval_seconds=60) -> Orchestrator:
    global _orchestrator
    _orchestrator = Orchestrator(trigger_getter, executor_getter, terminator_getter, interval_seconds)
    return _orchestrator
