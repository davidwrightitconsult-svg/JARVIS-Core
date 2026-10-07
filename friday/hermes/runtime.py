"""
Hermes runtime.

Phase 3B:
- submit(): validate untrusted request -> PENDING job (persisted, audited)
- run_next()/run_pending(): execute jobs one at a time
- Per-job timeout; bounded retries for transient failures only
- Budget exhaustion, authority violations, missing services and
  handler bugs fail immediately (never retried)
- Every state change is persisted and emitted as an audit event
- Optional background worker thread (start/stop)

Timeout note: Python cannot kill a thread. A timed-out handler is
abandoned (daemon thread) and the job is marked accordingly; its late
result is discarded.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from friday.core.events import Event

from .audit import job_event, rejection_event
from .authority import Capability, HermesAuthorityError
from .budget import BudgetExceededError, TokenBudget
from .handlers import HANDLERS, HandlerDeps, RetryableJobError
from .jobs import Job, JobRequest, JobStatus, HermesJobError, create_job, JOB_TIMEOUT_SECONDS
from .services import HermesServices, JobServices, ServiceUnavailableError
from .store import HermesStore

DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 120.0


class HermesRejectedError(ValueError):
    """A job request was refused; the rejection has been audited."""


class _Timeout(Exception):
    pass


class HermesRuntime:
    def __init__(
        self,
        store: HermesStore,
        budget: TokenBudget,
        services: HermesServices,
        event_sink: Callable[[Event], Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        on_job_finished: Callable[[dict[str, Any]], Any] | None = None,
        research_budget: TokenBudget | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.store = store
        self.budget = budget
        self.research_budget = research_budget
        self.services = services
        self._sink = event_sink or (lambda event: None)
        self.max_attempts = int(max_attempts)
        self.timeout_seconds = float(timeout_seconds)
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._intake_open = True
        self._intake_reason = ""
        self._on_finished = on_job_finished

    # ------------------------------------------------------------ audit

    def _emit(self, event: Event) -> None:
        try:
            self._sink(event)
        except Exception:
            # Audit sink failure must never crash Hermes, nor be silent:
            # the job row in the store remains the durable record.
            pass

    def _persist(self, job: Job, message: str | None = None) -> None:
        self.store.save_job(job)
        self._emit(job_event(job, message))
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            self._finished(job)

    def _finished(self, job: Job) -> None:
        """
        Phase 4B hook: hand a finished job to the knowledge recorder, but
        only if the job declared WRITE_KNOWLEDGE. Never raises.
        """
        if self._on_finished is None or Capability.WRITE_KNOWLEDGE not in job.capabilities:
            return
        try:
            self._on_finished({**job.summary(), "result": job.result})
        except Exception:
            pass

    # ------------------------------------------------------------ intake

    def submit(self, request: JobRequest | dict[str, Any]) -> Job:
        requested = (
            request.job_type if isinstance(request, JobRequest)
            else str((request or {}).get("job_type", ""))
        )
        requested_by = (
            request.requested_by if isinstance(request, JobRequest)
            else str((request or {}).get("requested_by", "UNKNOWN"))
        )
        try:
            job = create_job(request)
        except (HermesAuthorityError, HermesJobError) as exc:
            self._emit(rejection_event(requested, str(exc), requested_by))
            raise HermesRejectedError(str(exc)) from exc

        with self._lock:
            if not self._intake_open:
                reason = self._intake_reason or "Hermes is not accepting work."
                self._emit(rejection_event(requested, reason, requested_by))
                raise HermesRejectedError(reason)
            self._jobs[job.id] = job
        self._persist(job, f"{job.type.value} queued")
        self._queue.put(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                return False
            job.transition(JobStatus.CANCELLED, error="Cancelled")
        self._persist(job)
        return True

    def close_intake(self, reason: str) -> None:
        """Refuse all new submissions (kill switch). Atomic with submit()."""
        with self._lock:
            self._intake_open = False
            self._intake_reason = reason[:500]

    def open_intake(self) -> None:
        with self._lock:
            self._intake_open = True
            self._intake_reason = ""

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._intake_open

    def cancel_all_pending(self, reason: str = "Cancelled") -> int:
        """Cancel every queued job (used by the kill switch)."""
        with self._lock:
            pending = [j for j in self._jobs.values() if j.status is JobStatus.PENDING]
            for job in pending:
                job.transition(JobStatus.CANCELLED, error=reason[:500])
        for job in pending:
            self._persist(job)
        # Drain the queue; run_next would skip cancelled jobs anyway.
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        return len(pending)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def pending_count(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------ execution

    def _call_with_timeout(self, job: Job) -> dict[str, Any]:
        handler = HANDLERS[job.type]
        services = JobServices(job, self.services)
        deps = HandlerDeps(self.budget, self.research_budget)
        timeout = max(self.timeout_seconds, JOB_TIMEOUT_SECONDS.get(job.type, 0))
        box: dict[str, Any] = {}

        def target():
            try:
                box["result"] = handler(job, services, deps)
            except BaseException as exc:  # re-raised in the caller thread
                box["error"] = exc

        thread = threading.Thread(target=target, name=f"hermes-{job.id[:8]}", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise _Timeout(f"Timed out after {timeout:.0f}s")
        if "error" in box:
            raise box["error"]
        result = box.get("result")
        if not isinstance(result, dict):
            raise TypeError("Handler returned a non-dict result")
        result["execution_authorized"] = False
        return result

    def run_next(self, block: bool = False, timeout: float | None = None) -> Job | None:
        try:
            job_id = self._queue.get(block=block, timeout=timeout)
        except queue.Empty:
            return None

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.PENDING:
                return job  # cancelled while queued
            job.transition(JobStatus.RUNNING)
        self._persist(job)

        try:
            result = self._call_with_timeout(job)
        except (_Timeout, RetryableJobError) as exc:
            self._retry_or_fail(job, str(exc))
        except (BudgetExceededError, HermesAuthorityError, ServiceUnavailableError) as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self._fail(job, f"Handler error ({type(exc).__name__}): {exc}")
        else:
            with self._lock:
                job.transition(JobStatus.SUCCEEDED, result=result)
            self._persist(job)
        return job

    def _retry_or_fail(self, job: Job, reason: str) -> None:
        with self._lock:
            if job.attempts < self.max_attempts:
                job.transition(JobStatus.PENDING, error=reason)
                retry = True
            else:
                job.transition(JobStatus.FAILED, error=f"{reason} (after {job.attempts} attempts)")
                retry = False
        self._persist(job, f"{job.type.value} retry scheduled" if retry else None)
        if retry:
            self._queue.put(job.id)

    def _fail(self, job: Job, reason: str) -> None:
        with self._lock:
            job.transition(JobStatus.FAILED, error=reason)
        self._persist(job)

    def run_pending(self, max_jobs: int = 100) -> list[Job]:
        processed = []
        for _ in range(max_jobs):
            job = self.run_next(block=False)
            if job is None:
                break
            processed.append(job)
        return processed

    # ------------------------------------------------------------ worker

    def start(self) -> None:
        """
        Start a worker. Each start gets its own stop signal, so a previous
        worker still finishing a long job (e.g. after pause) can never
        swallow a resume: it exits after its current job, while the new
        worker takes over the queue.
        """
        with self._lock:
            if self._worker and self._worker.is_alive() and not self._stop.is_set():
                return
            self._stop = threading.Event()
            self._worker = threading.Thread(
                target=self._loop, args=(self._stop,), name="hermes-worker", daemon=True
            )
            self._worker.start()
        self._emit(Event(kind="HERMES", message="Hermes worker started",
                         source="HERMES", component="HERMES",
                         payload={"execution_authorized": False}))

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            stop, worker = self._stop, self._worker
        stop.set()
        if worker:
            worker.join(timeout)
        self._emit(Event(kind="HERMES", message="Hermes worker stopped",
                         source="HERMES", component="HERMES",
                         payload={"execution_authorized": False}))

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._worker and self._worker.is_alive() and not self._stop.is_set())

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_next(block=True, timeout=0.2)

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "worker_running": self.running,
            "pending": self.pending_count(),
            "budget": self.budget.status(),
            "research_budget": self.research_budget.status() if self.research_budget else None,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "execution_authorized": False,
        }
