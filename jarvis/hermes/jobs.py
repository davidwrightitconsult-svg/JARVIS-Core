"""
Hermes Job Contract.

Phase 3A:
- Job types, each declaring the capabilities it requires
- Registry verified at import time against Hermes' authority
- Strict, per-type parameter schemas for untrusted job requests
- Job lifecycle state machine with an explicit transition table
- Every job reports execution_authorized = False

A job describes work to route. It never authorizes execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jarvis.intelligence.tasks import DEFAULT_TASK, IntelligenceTask, resolve_task

from .authority import (
    HERMES_AUTHORITY,
    Capability,
    HermesAuthorityError,
    authorize,
    is_forbidden,
)


class HermesJobError(ValueError):
    """Raised when a job request or transition is invalid."""


# ---------------------------------------------------------------
# Job types and required capabilities
# ---------------------------------------------------------------

class JobType(str, Enum):
    STATUS_REPORT = "STATUS_REPORT"
    HEALTH_CHECK = "HEALTH_CHECK"
    MARKET_SCAN = "MARKET_SCAN"
    INTELLIGENCE_ANALYSIS = "INTELLIGENCE_ANALYSIS"
    VAULT_SYNC = "VAULT_SYNC"
    RESEARCH_INGEST_URL = "RESEARCH_INGEST_URL"
    RESEARCH_INGEST_INBOX = "RESEARCH_INGEST_INBOX"
    RESEARCH_DIGEST = "RESEARCH_DIGEST"


JOB_CAPABILITIES: Mapping[JobType, frozenset[Capability]] = MappingProxyType({
    JobType.STATUS_REPORT: frozenset({
        Capability.READ_STATE, Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
    JobType.HEALTH_CHECK: frozenset({
        Capability.READ_HEALTH, Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
    JobType.MARKET_SCAN: frozenset({
        Capability.READ_MARKET_DATA, Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
    JobType.INTELLIGENCE_ANALYSIS: frozenset({
        Capability.REQUEST_INTELLIGENCE,
        Capability.READ_STATE,
        Capability.READ_MARKET_DATA,          # 6C: recorded market data as context
        Capability.READ_KNOWLEDGE,
        Capability.READ_RESEARCH,
        Capability.EMIT_EVENT,
        Capability.WRITE_KNOWLEDGE,
    }),
    # Deliberately NOT WRITE_KNOWLEDGE: a sync must not journal itself,
    # or every sync would create the change the next sync commits.
    JobType.VAULT_SYNC: frozenset({Capability.PUBLISH_KNOWLEDGE, Capability.EMIT_EVENT}),
    JobType.RESEARCH_INGEST_URL: frozenset({
        Capability.INGEST_RESEARCH, Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
    JobType.RESEARCH_INGEST_INBOX: frozenset({
        Capability.INGEST_RESEARCH, Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
    # Reads library text, asks Claude (research budget), records progress in
    # the library and writes the digest note to FRIDAY/Research/.
    JobType.RESEARCH_DIGEST: frozenset({
        Capability.READ_RESEARCH, Capability.INGEST_RESEARCH, Capability.REQUEST_INTELLIGENCE,
        Capability.EMIT_EVENT, Capability.WRITE_KNOWLEDGE}),
})

# Jobs that legitimately run longer than the global per-job timeout.
JOB_TIMEOUT_SECONDS: Mapping[JobType, int] = MappingProxyType({
    JobType.RESEARCH_DIGEST: 900,
})


# ---------------------------------------------------------------
# Per-type parameter schemas (untrusted input)
# ---------------------------------------------------------------

class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StatusReportParams(_Params):
    pass


class HealthCheckParams(_Params):
    pass


class MarketScanParams(_Params):
    limit: int = Field(default=25, ge=1, le=100)


class VaultSyncParams(_Params):
    pass


_RESEARCH_KINDS = ("book", "paper", "note", "article")


class ResearchIngestUrlParams(_Params):
    url: str = Field(min_length=9, max_length=2000)
    kind: str = "article"
    title: str | None = Field(default=None, max_length=200)
    tags: tuple[str, ...] = Field(default=(), max_length=10)
    refresh: bool = False

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.strip().lower().startswith("https://"):
            raise ValueError("only https:// URLs are allowed")
        return value.strip()

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in _RESEARCH_KINDS:
            raise ValueError(f"kind must be one of {', '.join(_RESEARCH_KINDS)}")
        return value


class ResearchIngestInboxParams(_Params):
    pass


class ResearchDigestParams(_Params):
    doc_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    sections: int = Field(default=6, ge=1, le=10)
    start: int | None = Field(default=None, ge=0, le=100_000)


class IntelligenceAnalysisParams(_Params):
    task: IntelligenceTask = DEFAULT_TASK
    user_command: str = Field(min_length=1, max_length=4000)
    market_context: dict[str, Any] = Field(default_factory=dict)
    research_context: dict[str, Any] = Field(default_factory=dict)
    strategy_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task", mode="before")
    @classmethod
    def _resolve_task(cls, value):
        return resolve_task(value)


JOB_PARAMS: Mapping[JobType, type[_Params]] = MappingProxyType({
    JobType.STATUS_REPORT: StatusReportParams,
    JobType.HEALTH_CHECK: HealthCheckParams,
    JobType.MARKET_SCAN: MarketScanParams,
    JobType.INTELLIGENCE_ANALYSIS: IntelligenceAnalysisParams,
    JobType.VAULT_SYNC: VaultSyncParams,
    JobType.RESEARCH_INGEST_URL: ResearchIngestUrlParams,
    JobType.RESEARCH_INGEST_INBOX: ResearchIngestInboxParams,
    JobType.RESEARCH_DIGEST: ResearchDigestParams,
})


def _verify_registry() -> None:
    for job_type in JobType:
        if job_type not in JOB_CAPABILITIES or job_type not in JOB_PARAMS:
            raise RuntimeError(f"Job type {job_type.value} is not fully registered.")
        if not JOB_CAPABILITIES[job_type] <= HERMES_AUTHORITY:
            raise RuntimeError(f"Job type {job_type.value} exceeds Hermes' authority.")
        if is_forbidden(job_type.value):
            raise RuntimeError(f"Job type {job_type.value} matches a forbidden action.")


_verify_registry()


def resolve_job_type(name: JobType | str) -> JobType:
    if isinstance(name, JobType):
        return name
    if is_forbidden(name):
        raise HermesAuthorityError(
            f"'{name}' is outside Hermes' authority; no such job can exist."
        )
    try:
        return JobType(str(name).strip().upper())
    except ValueError:
        raise HermesJobError(f"Unknown job type: {name!r}") from None


# ---------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

ALLOWED_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = MappingProxyType({
    JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    # RUNNING -> PENDING is a bounded retry (3B enforces the attempt limit).
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.PENDING,
    }),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    """
    A unit of routed work. Mutated only through transition().
    """

    type: JobType
    params: _Params
    requested_by: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def capabilities(self) -> frozenset[Capability]:
        return JOB_CAPABILITIES[self.type]

    def transition(
        self,
        new_status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise HermesJobError(
                f"Illegal job transition {self.status.value} -> {new_status.value}"
            )
        if new_status is JobStatus.RUNNING:
            self.attempts += 1
        if result is not None:
            self.result = dict(result)
        if error is not None:
            self.error = error
        self.history.append((self.status.value, new_status.value))
        self.status = new_status
        self.updated_at = _now()

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "job_type": self.type.value,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "attempts": self.attempts,
            "correlation_id": self.correlation_id,
            "capabilities": sorted(c.value for c in self.capabilities),
            "error": self.error,
            "execution_authorized": False,
        }


# ---------------------------------------------------------------
# Untrusted request -> validated Job
# ---------------------------------------------------------------

class JobRequest(BaseModel):
    """
    Untrusted job request (UI, voice, API, or a Claude suggestion).
    """

    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="SYSTEM", min_length=1, max_length=64)


def create_job(request: JobRequest | dict[str, Any]) -> Job:
    """
    Validate an untrusted request and build a PENDING job.

    Raises HermesAuthorityError for anything naming a forbidden action,
    HermesJobError for any other invalid request.
    """

    try:
        req = request if isinstance(request, JobRequest) else JobRequest.model_validate(request)
    except ValidationError as exc:
        raise HermesJobError(f"Invalid job request: {exc}") from None

    job_type = resolve_job_type(req.job_type)
    authorize(*JOB_CAPABILITIES[job_type])

    try:
        params = JOB_PARAMS[job_type].model_validate(req.params)
    except (ValidationError, ValueError) as exc:
        raise HermesJobError(f"Invalid parameters for {job_type.value}: {exc}") from None

    return Job(type=job_type, params=params, requested_by=req.requested_by)
