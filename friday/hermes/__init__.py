"""
Hermes — FRIDAY's operator layer.

Hermes schedules, routes, observes and reports.
Hermes has no execution, risk, bankroll, wallet or credential authority.
"""

from .authority import (
    HERMES_AUTHORITY,
    Capability,
    HermesAuthorityError,
    authorize,
    is_forbidden,
)
from .audit import job_event, rejection_event
from .budget import BudgetExceededError, TokenBudget
from .control import HermesControlError, HermesController, HermesState
from .runtime import HermesRejectedError, HermesRuntime
from .scheduler import HermesScheduleError, HermesScheduler, MIN_INTERVAL_SECONDS, Schedule
from .services import HermesServices
from .store import HermesStore
from .jobs import (
    JOB_CAPABILITIES,
    Job,
    JobRequest,
    JobStatus,
    JobType,
    HermesJobError,
    create_job,
)

HERMES_CONTRACT_VERSION = "3A.1"
HERMES_RUNTIME_VERSION = "3B.1"
HERMES_SCHEDULER_VERSION = "3C.1"

__all__ = [
    "HERMES_SCHEDULER_VERSION",
    "HermesControlError",
    "HermesController",
    "HermesScheduleError",
    "HermesScheduler",
    "HermesState",
    "MIN_INTERVAL_SECONDS",
    "Schedule",
    "BudgetExceededError",
    "HERMES_RUNTIME_VERSION",
    "HermesRejectedError",
    "HermesRuntime",
    "HermesServices",
    "HermesStore",
    "TokenBudget",
    "HERMES_AUTHORITY",
    "HERMES_CONTRACT_VERSION",
    "Capability",
    "HermesAuthorityError",
    "HermesJobError",
    "JOB_CAPABILITIES",
    "Job",
    "JobRequest",
    "JobStatus",
    "JobType",
    "authorize",
    "create_job",
    "is_forbidden",
    "job_event",
    "rejection_event",
]
