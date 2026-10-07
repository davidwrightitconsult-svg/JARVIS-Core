from .events import Event


class LifecycleManager:
    def __init__(self, state_manager, event_logger, hermes=None):
        """
        hermes: optional HermesController (Phase 3C). When supplied it is
        started after the control plane and stopped before it, and its
        state is reported as the 'hermes' component.
        """
        self.state = state_manager
        self.events = event_logger
        self.hermes = hermes
        self.started = False

    def start(self):
        if self.started:
            return

        self.state.lock_execution()
        self.state.start()

        self.state.set_component("database", "OK")
        self.state.set_component("command_router", "OK")
        self.state.set_component("risk_governor", "READY")
        self.state.set_component("execution_gate", "LOCKED")

        self.events.emit(
            Event(
                kind="LIFECYCLE",
                message="FRIDAY control plane started",
                source="SYSTEM",
                component="LIFECYCLE",
            )
        )

        self.started = True

        if self.hermes is not None:
            try:
                self.hermes.start()
            except Exception as exc:  # Hermes must never block FRIDAY start-up
                self.state.record_error(f"Hermes failed to start: {exc}")
            self.state.set_component("hermes", self.hermes.component_status())

    def stop(self):
        if not self.started:
            return

        if self.hermes is not None:
            try:
                self.hermes.stop()
            finally:
                self.state.set_component("hermes", self.hermes.component_status())

        self.events.emit(
            Event(
                kind="LIFECYCLE",
                message="FRIDAY control plane stopping",
                source="SYSTEM",
                component="LIFECYCLE",
            )
        )

        self.state.lock_execution()
        self.state.stop()
        self.started = False
