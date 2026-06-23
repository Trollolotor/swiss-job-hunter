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
    job.expired_at = checked_at
    if job.status not in PROTECTED_STATUSES:
        job.status = JobStatus.ARCHIVED
    return True
