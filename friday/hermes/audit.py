"""
Hermes audit events.

Every Hermes state change is expressed as a FRIDAY control-plane Event,
so it lands in the same events table as lifecycle and command events.
Every Hermes event carries execution_authorized = False.
"""

from __future__ import annotations

from friday.core.events import Event

from .jobs import Job, JobStatus

HERMES_COMPONENT = "HERMES"
HERMES_EVENT_KIND = "HERMES_JOB"

_SEVERITY = {
    JobStatus.PENDING: "INFO",
    JobStatus.RUNNING: "INFO",
    JobStatus.SUCCEEDED: "INFO",
    JobStatus.FAILED: "WARNING",
    JobStatus.CANCELLED: "WARNING",
}


def job_event(job: Job, message: str | None = None) -> Event:
    """
    Build the audit event for a job's current state.
    """

    return Event(
        kind=HERMES_EVENT_KIND,
        message=message or f"{job.type.value} {job.status.value}",
        source="HERMES",
        component=HERMES_COMPONENT,
        severity=_SEVERITY[job.status],
        correlation_id=job.correlation_id,
        payload=job.summary(),
    )


def rejection_event(requested: str, reason: str, requested_by: str = "UNKNOWN") -> Event:
    """
    Audit event for a job request Hermes refused to create.
    """

    return Event(
        kind="HERMES_REJECTED",
        message=f"Rejected job request '{requested}'",
        source="HERMES",
        component=HERMES_COMPONENT,
        severity="WARNING",
        payload={
            "requested": str(requested)[:64],
            "requested_by": str(requested_by)[:64],
            "reason": reason[:500],
            "execution_authorized": False,
        },
    )
