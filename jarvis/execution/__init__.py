"""JARVIS Execution Layer — the autonomous trading agents.

Named agents:
  TRIGGER      — 24/7 loop that fires strategy evaluations
  EXECUTOR     — places trades through the Risk Governor
  ORCHESTRATOR — health monitor, auto-reconnect, crash recovery
  SETTLEMENT   — pre-resolution exits for binary markets
  BROKER       — abstract broker interface (Paper now, live later)
"""

from .trigger import Trigger, get_trigger
from .executor import Executor, get_executor
from .orchestrator import Orchestrator, get_orchestrator
from .settlement import Settlement, get_settlement
from .broker import Broker, PaperBroker

__all__ = [
    "Trigger", "get_trigger",
    "Executor", "get_executor",
    "Orchestrator", "get_orchestrator",
    "Settlement", "get_settlement",
    "Broker", "PaperBroker",
]
