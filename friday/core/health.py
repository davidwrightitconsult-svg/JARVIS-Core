from datetime import datetime, timezone

HEALTHY_COMPONENT_STATES = {
    "OK",
    "ONLINE",
    "READY",
    "LOCKED",
    "DISABLED",
    # Phase 3C — Hermes operator states.
    # RUNNING and PAUSED (a deliberate operator choice) are healthy.
    # STOPPED while FRIDAY is online, or KILLED, reports DEGRADED so the
    # condition is visible on the dashboard.
    "RUNNING",
    "PAUSED",
}

class HealthManager:
    def __init__(self, state_manager):
        self.state_manager = state_manager

    def check(self) -> dict:
        state = self.state_manager.snapshot()
        components = state["components"]

        component_ok = (
            all(
                value in HEALTHY_COMPONENT_STATES
                for value in components.values()
            )
            if components
            else True
        )

        healthy = (
            state["status"] == "ONLINE"
            and component_ok
            and state["last_error"] is None
        )

        return {
            "status": "HEALTHY" if healthy else "DEGRADED",
            "name": "FRIDAY",
            "mode": state["mode"],
            "execution_locked": state["execution_locked"],
            "components": components,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "last_error": state["last_error"],
        }
