"""Consistent vacancy availability transitions."""
from __future__ import annotations

from datetime import datetime

from db.models import JobStatus

PROTECTED_STATUSES = {JobStatus.APPLIED, JobStatus.INTERVIEWING, JobStatus.OFFER}


def mark_available(job, now: datetime | None = None) -> None:
    job.availability_checked_at = now or datetime.utcnow()
    job.unavailable_checks = 0
    job.expired_at = None
    job.expired_reason = None


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
    if job.status not in PROTECTED_STATUSES:
        job.status = JobStatus.ARCHIVED
    return True


def set_scored_status(job, status) -> None:
    """Automated scoring must not overwrite manual review/application decisions."""
    if job.status in {JobStatus.NEW, JobStatus.ANALYZED}:
        job.status = status
