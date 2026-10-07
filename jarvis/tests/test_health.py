from jarvis.app.db import EventLogger
from jarvis.core.health import HealthManager
from jarvis.core.lifecycle import LifecycleManager
from jarvis.core.state import StateManager


class NullLogger:
    def emit(self, event):
        return event.normalized()["event_id"]


def test_health_after_start():
    state = StateManager("PAPER")
    lifecycle = LifecycleManager(state, NullLogger())

    lifecycle.start()

    result = HealthManager(state).check()

    assert result["status"] == "HEALTHY"
    assert result["execution_locked"] is True
