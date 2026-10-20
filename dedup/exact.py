"""
Exact deduplication — hash(title + company + location).
Fast, runs on every insert.
"""
from __future__ import annotations

import re
from datetime import datetime
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


def get_or_create_job(
    scraped: ScrapedJob,
    direction: Optional[str] = None,
    profile_ids: Optional[list[int]] = None,
) -> tuple[Job, bool]:
    """
    Return (job, created).
    The returned Job is expunged from the session so it can be safely
    used after the session closes — no DetachedInstanceError.
    """
    # Source identity is a correctness primitive even when paid employer discovery
    # is disabled. Existing canonical IDs/hashes are retained by exact source lookup.
    from employers.ingestion import ingest_board
    return ingest_board(scraped, direction, profile_ids)


def _date_confidence(source: str | None) -> float:
    return {
        "api": 1.0, "json_ld": 0.95, "embedded_json": 0.9,
        "meta": 0.85, "html_time": 0.8, "html": 0.7,
        "relative_text": 0.5,
    }.get(source or "unknown", 0.0)
