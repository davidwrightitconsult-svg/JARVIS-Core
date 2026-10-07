from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Dict


@dataclass
class FridayState:
    status: str = "STARTING"
    mode: str = "PAPER"
    execution_locked: bool = True
    started_at: str | None = None
    stopped_at: str | None = None
    last_command: str | None = None
    last_command_source: str | None = None
    last_error: str | None = None
    components: Dict[str, str] = field(default_factory=dict)


class StateManager:
    def __init__(self, mode: str = "PAPER"):
        self._lock = RLock()
        self._state = FridayState(mode=mode.upper())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._state.status,
                "mode": self._state.mode,
                "execution_locked": self._state.execution_locked,
                "started_at": self._state.started_at,
                "stopped_at": self._state.stopped_at,
                "last_command": self._state.last_command,
                "last_command_source": self._state.last_command_source,
                "last_error": self._state.last_error,
                "components": dict(self._state.components),
            }

    def start(self):
        with self._lock:
            self._state.status = "ONLINE"
            self._state.started_at = datetime.now(timezone.utc).isoformat()
            self._state.stopped_at = None

    def stop(self):
        with self._lock:
            self._state.status = "STOPPED"
            self._state.stopped_at = datetime.now(timezone.utc).isoformat()

    def set_component(self, name: str, status: str):
        with self._lock:
            self._state.components[name] = status

    def record_command(self, command: str, source: str):
        with self._lock:
            self._state.last_command = command
            self._state.last_command_source = source

    def record_error(self, message: str):
        with self._lock:
            self._state.last_error = message

    def clear_error(self):
        with self._lock:
            self._state.last_error = None

    def lock_execution(self):
        with self._lock:
            self._state.execution_locked = True

    def is_execution_locked(self) -> bool:
        with self._lock:
            return self._state.execution_locked
