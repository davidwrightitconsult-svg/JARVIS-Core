from friday.app.db import EventLogger
from friday.core.health import HealthManager
from friday.core.lifecycle import LifecycleManager
from friday.core.state import StateManager


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
