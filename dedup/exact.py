"""
Exact deduplication — hash(title + company + location).
Fast, runs on every insert.
"""
from __future__ import annotations

import re
from typing import Optional

from db.models import Job
from db.session import get_session
from scrapers.base import ScrapedJob


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_exact_duplicate(title: str, company: str, location: str) -> bool:
    """Check if a job with this hash already exists in the DB."""
    h = Job.make_dedup_hash(
        _normalize(title), _normalize(company), _normalize(location)
    )
    with get_session() as session:
        return session.query(Job).filter(Job.dedup_hash == h).count() > 0


def get_or_create_job(scraped: ScrapedJob, direction: Optional[str] = None) -> tuple[Job, bool]:
    """
    Return (job, created).
    The returned Job is expunged from the session so it can be safely
    used after the session closes — no DetachedInstanceError.
    """
    h = Job.make_dedup_hash(
        _normalize(scraped.title),
        _normalize(scraped.company),
        _normalize(scraped.location),
    )

    with get_session() as session:
        existing = session.query(Job).filter(Job.dedup_hash == h).first()
        if existing:
            if not existing.posted_at and scraped.posted_at:
                existing.posted_at = scraped.posted_at
                existing.posted_at_source = scraped.posted_at_source
            if direction:
                from db.models import JobProfile, SearchProfile
                profile = session.query(SearchProfile).filter_by(slug=direction).first()
                if profile and not session.get(JobProfile, (existing.id, profile.id)):
                    session.add(JobProfile(job_id=existing.id, profile_id=profile.id))
                    session.flush()
            session.expunge(existing)
            return existing, False

        job = Job(
            dedup_hash=h,
            title=scraped.title,
            company=scraped.company,
            location=scraped.location,
            description=scraped.description,
            url=scraped.url,
            source=scraped.source,
            source_job_id=scraped.source_job_id,
            salary_raw=scraped.salary_raw,
            employment_type=scraped.employment_type,
            remote_ok=scraped.remote_ok,
            language_required=scraped.language_required,
            posted_at=scraped.posted_at,
            posted_at_source=scraped.posted_at_source,
            direction=direction,
        )
        session.add(job)
        session.flush()
        if direction:
            from db.models import JobProfile, SearchProfile
            profile = session.query(SearchProfile).filter_by(slug=direction).first()
            if profile:
                session.add(JobProfile(job_id=job.id, profile_id=profile.id))
        session.refresh(job)
        session.expunge(job)
        return job, True
