"""Consistent vacancy availability transitions."""
from __future__ import annotations

from datetime import datetime

from db.models import JobStatus

PROTECTED_STATUSES = {JobStatus.APPLIED, JobStatus.INTERVIEWING, JobStatus.OFFER}


def transition(job, status, *, origin, reason=None):
    """All automatic and manual workflow writes share one ownership policy."""
    from sqlalchemy.orm import object_session
    status = JobStatus(status)
    current = JobStatus(job.status)
    owner = getattr(job, "status_origin", "legacy_unknown")
    if origin != "user":
        if current not in {JobStatus.NEW, JobStatus.ANALYZED, JobStatus.SHORTLISTED, JobStatus.ARCHIVED}:
            return False
        if owner == "user" or (owner == "legacy_unknown" and current in {
            JobStatus.ARCHIVED, JobStatus.SHORTLISTED
        }):
            return False
    session = object_session(job) if hasattr(job, "_sa_instance_state") else None
    if session and (current != status or owner != origin):
        from db.models import JobStatusChange
        session.add(JobStatusChange(job_id=job.id, previous=current.value, status=status.value,
                                    origin=origin, reason=(reason or "")[:2000] or None))
    job.status, job.status_origin = status, origin
    return current != status


def mark_available(job, now: datetime | None = None) -> None:
    job.availability_checked_at = now or datetime.utcnow()
    job.unavailable_checks = 0
    job.expired_at = None
    job.expired_reason = None
    if job.status == JobStatus.ARCHIVED and getattr(job, "status_origin", None) == "availability":
        transition(job, JobStatus.NEW, origin="availability", reason="Vacancy observed open again")


def mark_unavailable(job, reason: str, now: datetime | None = None) -> bool:
    checked_at = now or datetime.utcnow()
    job.availability_checked_at = checked_at
    job.unavailable_checks = (job.unavailable_checks or 0) + 1
    job.expired_reason = reason
    if job.unavailable_checks < 2:
        return False
    # With source provenance enabled, a closed board copy cannot close a live
    # employer posting or rewrite a user's application workflow.
    from sqlalchemy.orm import object_session
    session = object_session(job) if hasattr(job, "_sa_instance_state") else None
    if session is not None:
        from employers.config import enabled
        if enabled():
            from db.models import JobSource
            rows = session.query(JobSource).filter_by(job_id=job.id).all()
            for row in rows:
                if row.namespace == f"board:{job.source}":
                    row.availability, row.checked_at = "closed", checked_at
            if rows and any(row.availability != "closed" for row in rows):
                return False
            job.expired_at = checked_at
            return True
    job.expired_at = checked_at
    transition(job, JobStatus.ARCHIVED, origin="availability", reason=reason)
    return True


def set_scored_status(job, status) -> None:
    """Automated scoring must not overwrite manual review/application decisions."""
    return transition(job, status, origin="scoring", reason="Profile evaluation updated")
