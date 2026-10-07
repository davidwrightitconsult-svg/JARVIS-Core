"""
Hermes scheduler.

Phase 3C:
- Recurring schedules, each wrapping an ordinary Hermes job request
- Schedules are validated like any untrusted job request (forbidden
  job types are impossible to schedule)
- Minimum interval per job type, so Hermes cannot hammer an API
  or burn the Claude budget
- No pile-up: a schedule does not submit a new job while its previous
  job is still pending or running
- Deterministic tick(now) for tests; background thread for runtime
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from friday.core.events import Event

from .jobs import HermesJobError, JobRequest, JobStatus, JobType, create_job, resolve_job_type
from .runtime import HermesRejectedError, HermesRuntime

# Minimum seconds between runs of each job type.
MIN_INTERVAL_SECONDS: Mapping[JobType, int] = MappingProxyType({
    JobType.STATUS_REPORT: 30,
    JobType.HEALTH_CHECK: 30,
    JobType.MARKET_SCAN: 300,
    JobType.INTELLIGENCE_ANALYSIS: 900,
    JobType.VAULT_SYNC: 300,
    JobType.RESEARCH_INGEST_URL: 3600,     # a scheduled URL re-fetch at most hourly
    JobType.RESEARCH_INGEST_INBOX: 300,
    JobType.RESEARCH_DIGEST: 3600,
})

MAX_SCHEDULES = 20
MAX_INTERVAL_SECONDS = 7 * 24 * 3600

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class HermesScheduleError(ValueError):
    """Invalid schedule definition."""


class Schedule(BaseModel):
    """
    Untrusted schedule definition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    job: JobRequest
    interval_seconds: int = Field(ge=1, le=MAX_INTERVAL_SECONDS)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError("name must be 1-48 chars: lowercase letters, digits, '-', '_'")
        return value


def _verify_minimums() -> None:
    missing = set(JobType) - set(MIN_INTERVAL_SECONDS)
    if missing:
        raise RuntimeError(f"No minimum interval for {sorted(m.value for m in missing)}")


_verify_minimums()


def validate_schedule(definition: Schedule | dict[str, Any]) -> Schedule:
    try:
        schedule = definition if isinstance(definition, Schedule) else Schedule.model_validate(definition)
    except ValidationError as exc:
        raise HermesScheduleError(f"Invalid schedule: {exc}") from None

    # Full job validation (authority, params) up front — raises
    # HermesAuthorityError for forbidden job types.
    try:
        create_job(schedule.job)
    except HermesJobError as exc:
        raise HermesScheduleError(f"Schedule '{schedule.name}' has an invalid job: {exc}") from None

    job_type = resolve_job_type(schedule.job.job_type)
    minimum = MIN_INTERVAL_SECONDS[job_type]
    if schedule.interval_seconds < minimum:
        raise HermesScheduleError(
            f"Schedule '{schedule.name}': {job_type.value} interval must be >= {minimum}s"
        )
    return schedule


@dataclass
class _Entry:
    schedule: Schedule
    next_due: float
    last_job_id: str | None = None
    runs: int = 0
    skipped: int = 0


class HermesScheduler:
    def __init__(
        self,
        runtime: HermesRuntime,
        clock: Callable[[], float] = time.monotonic,
        tick_seconds: float = 1.0,
    ):
        self.runtime = runtime
        self._clock = clock
        self.tick_seconds = float(tick_seconds)
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ registry

    def add(self, definition: Schedule | dict[str, Any], run_immediately: bool = True) -> Schedule:
        schedule = validate_schedule(definition)
        with self._lock:
            if schedule.name not in self._entries and len(self._entries) >= MAX_SCHEDULES:
                raise HermesScheduleError(f"At most {MAX_SCHEDULES} schedules")
            now = self._clock()
            due = now if run_immediately else now + schedule.interval_seconds
            self._entries[schedule.name] = _Entry(schedule=schedule, next_due=due)
        self.runtime._emit(Event(
            kind="HERMES_SCHEDULE", message=f"Schedule '{schedule.name}' registered",
            source="HERMES", component="HERMES",
            payload={"name": schedule.name, "job_type": schedule.job.job_type,
                     "interval_seconds": schedule.interval_seconds,
                     "execution_authorized": False},
        ))
        return schedule

    def remove(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def list(self) -> list[dict[str, Any]]:
        now = self._clock()
        with self._lock:
            return [
                {
                    "name": e.schedule.name,
                    "job_type": e.schedule.job.job_type,
                    "interval_seconds": e.schedule.interval_seconds,
                    "enabled": e.schedule.enabled,
                    "runs": e.runs,
                    "skipped_busy": e.skipped,
                    "due_in_seconds": max(0.0, round(e.next_due - now, 1)),
                    "last_job_id": e.last_job_id,
                }
                for e in self._entries.values()
            ]

    # ------------------------------------------------------------ ticking

    def _busy(self, entry: _Entry) -> bool:
        if entry.last_job_id is None:
            return False
        job = self.runtime.get(entry.last_job_id)
        return job is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING)

    def tick(self, now: float | None = None) -> list[str]:
        """
        Submit every due schedule. Returns the job ids submitted.
        """

        now = self._clock() if now is None else now
        submitted: list[str] = []
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            if not entry.schedule.enabled or now < entry.next_due:
                continue
            entry.next_due = now + entry.schedule.interval_seconds
            if self._busy(entry):
                entry.skipped += 1
                continue
            request = entry.schedule.job.model_copy(
                update={"requested_by": f"SCHEDULER:{entry.schedule.name}"[:64]}
            )
            try:
                job = self.runtime.submit(request)
            except HermesRejectedError:
                continue  # already audited by the runtime
            entry.last_job_id = job.id
            entry.runs += 1
            submitted.append(job.id)
        return submitted

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, args=(self._stop,), name="hermes-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
        stop.set()
        if thread:
            thread.join(timeout)

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # a bad tick must not kill the scheduler; jobs are audited
            stop.wait(self.tick_seconds)
