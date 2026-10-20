"""Trusted source identity, conservative linkage, and authoritative enrichment."""

import json
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import or_, select

from db.models import Job, JobProfile, JobSource, JobSourceRevision, SearchProfile
from employers.contracts import CollectedPosting, digest


def ingest(
    session,
    scraped,
    *,
    source=None,
    employer_id=None,
    posting=None,
    canonical_id=None,
    profile_ids=None,
    direction=None,
    historical=False,
    observed_at=None,
):
    if scraped.posted_at and scraped.posted_at.tzinfo:
        scraped = replace(
            scraped, posted_at=scraped.posted_at.astimezone(timezone.utc).replace(tzinfo=None)
        )
    posting = posting or CollectedPosting(
        job=scraped,
        posting_id=scraped.source_job_id or scraped.url,
        application_url=scraped.extra.get("application_url"),
        requisition_id=scraped.extra.get("requisition_id"),
    )
    if source:
        # Canonical compatibility identity is selected by the controller. Agent
        # labels and requisition IDs cannot replace the source posting identity.
        from bs4 import BeautifulSoup

        text = scraped.description
        soup = BeautifulSoup(text, "html.parser")
        if soup.find(["p", "div", "li", "br", "ul", "ol", "strong", "em", "span"]):
            text = soup.get_text("\n", strip=True)
        scraped = replace(
            scraped, source=source.platform, source_job_id=posting.posting_id, description=text
        )
        posting = posting.model_copy(update={"job": scraped})
    namespace = source.namespace if source else f"board:{scraped.source}"
    employer_id = source.employer_id if source else employer_id
    identity = (namespace, posting.posting_id, posting.variant)
    record = session.scalar(
        select(JobSource).where(
            JobSource.namespace == namespace,
            JobSource.posting_id == posting.posting_id,
            JobSource.variant == posting.variant,
        )
    )
    job = session.get(Job, record.job_id) if record and record.job_id else None
    if not job and canonical_id:
        job = session.get(Job, canonical_id)
    if not job and not source:
        # Preserve existing canonical IDs by exact historical source identity only.
        predicates = [Job.url == scraped.url] if scraped.url else []
        if scraped.source_job_id:
            predicates.append(Job.source_job_id == scraped.source_job_id)
        job = session.scalar(
            select(Job).where(Job.source == scraped.source, or_(*predicates)).order_by(Job.id)
        )
    if not job and employer_id:
        conditions = []
        if posting.requisition_id:
            conditions.append(JobSource.requisition_id == posting.requisition_id)
        if posting.application_url:
            conditions.append(JobSource.application_url == posting.application_url)
        if conditions:
            ids = session.scalars(
                select(JobSource.job_id)
                .where(
                    JobSource.employer_id == employer_id,
                    JobSource.job_id.is_not(None),
                    or_(*conditions),
                )
                .distinct()
            ).all()
            if len(ids) == 1:
                job = session.get(Job, ids[0])
    created = job is None

    def canonical_content(row):
        return digest(
            [row.title, row.description, row.location, row.employment_type, row.salary_raw]
        )

    previous_canonical = canonical_content(job) if job else None
    now = observed_at or datetime.utcnow()
    if created:
        job = Job(
            dedup_hash=digest(["source-v1", *identity]),
            title=scraped.title,
            company=scraped.company,
            location=scraped.location,
            description=scraped.description,
            url=scraped.url,
            source=scraped.source,
            source_job_id=scraped.source_job_id,
            posted_at=scraped.posted_at,
            posted_at_source=scraped.posted_at_source,
            posted_at_raw=scraped.posted_at_raw,
            posted_at_confidence=scraped.posted_at_confidence,
            salary_raw=scraped.salary_raw,
            employment_type=scraped.employment_type,
            remote_ok=scraped.remote_ok,
            language_required=scraped.language_required,
            direction=direction,
            last_seen_at=now,
        )
        session.add(job)
        session.flush()
    if not record:
        record = JobSource(
            namespace=namespace,
            posting_id=posting.posting_id,
            variant=posting.variant,
            job_id=job.id,
            employer_id=employer_id,
            source_id=source.id if source else None,
            url=scraped.url,
            first_seen_at=now,
            historical=historical,
        )
        session.add(record)
        session.flush()
    record.job_id = job.id
    record.employer_id = record.employer_id or employer_id
    # Do not let sparse legacy imports overwrite a newer source observation.
    if historical and record.last_seen_at and record.last_seen_at > now:
        return job, created, record
    payload = posting.model_dump(mode="json")
    # Raw diagnostic bytes belong outside the normalized source record.
    payload["job"].pop("raw_html", None)
    payload["job"].pop("raw_json", None)
    content_hash = digest(
        {
            k: payload["job"].get(k)
            for k in ("title", "description", "location", "employment_type", "salary_raw")
        }
    )
    if record.content_hash and record.content_hash != content_hash and scraped.description:
        record.content_changed_at = now
    if record.content_hash != content_hash and scraped.description:
        session.add(
            JobSourceRevision(
                job_source_id=record.id,
                content_hash=content_hash,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        )
    if scraped.description or not record.content_hash:
        record.content_hash, record.payload_json = (
            content_hash,
            json.dumps(payload, ensure_ascii=False),
        )
    record.url = scraped.url
    record.application_url = posting.application_url or record.application_url
    record.requisition_id = posting.requisition_id or record.requisition_id
    record.last_seen_at = now
    if not historical:
        record.availability, record.missing_checks, record.checked_at = "open", 0, now
        job.last_seen_at, job.expired_at, job.expired_reason = now, None, None
        from job_lifecycle import mark_available

        mark_available(job, now)
    if source and scraped.description and not historical:
        from analyzer.quality import set_description

        set_description(
            session,
            job,
            scraped.description,
            trusted_detail=scraped.extra.get("description_quality") == "complete",
            declared=scraped.extra.get("description_quality"),
            source=namespace,
            evidence=posting.evidence,
        )
        if scraped.title:
            job.title = scraped.title
        if scraped.location:
            job.location = scraped.location
        if job.source == scraped.source and job.source_job_id == scraped.source_job_id:
            job.url = scraped.url
        for attr in ("salary_raw", "employment_type", "remote_ok", "language_required"):
            value = getattr(scraped, attr)
            if value is not None and value != "":
                setattr(job, attr, value)
    elif (
        not historical
        and scraped.description
        and len(scraped.description) > len(job.description or "")
        and board_may_enrich(session, job.id)
    ):
        from analyzer.quality import set_description

        set_description(
            session,
            job,
            scraped.description,
            trusted_detail=scraped.extra.get("description_quality") == "complete",
            declared=scraped.extra.get("description_quality"),
            source=namespace,
            evidence=[scraped.url],
        )
    if (
        previous_canonical is not None
        and canonical_content(job) != previous_canonical
        and not historical
    ):
        record.content_changed_at, record.score_stale = now, True
        session.query(JobProfile).filter_by(job_id=job.id).update(
            {JobProfile.score_stale: True}, synchronize_session=False
        )
    if scraped.posted_at and (
        not job.posted_at or scraped.posted_at_confidence > (job.posted_at_confidence or 0)
    ):
        job.posted_at, job.posted_at_source = scraped.posted_at, scraped.posted_at_source
        job.posted_at_raw, job.posted_at_confidence = (
            scraped.posted_at_raw,
            scraped.posted_at_confidence,
        )
    ids = list(profile_ids or [])
    if direction and not ids:
        p = session.scalar(select(SearchProfile).where(SearchProfile.slug == direction))
        if p:
            ids.append(p.id)
    for profile_id in ids:
        if not session.get(JobProfile, (job.id, profile_id)):
            session.add(JobProfile(job_id=job.id, profile_id=profile_id))
    session.flush()
    return job, created, record


def ingest_board(scraped, direction=None, profile_ids=None):
    from db.session import get_session
    from employers.discovery import observe

    with get_session() as session:
        from employers.config import enabled

        employer = observe(session, scraped) if enabled() else None
        job, created, _ = ingest(
            session,
            scraped,
            direction=direction,
            profile_ids=profile_ids,
            employer_id=employer.id if employer else None,
        )
        session.expunge(job)
        return job, created


def job_source_fields(session, job_id, rows=None):
    if rows is None:
        rows = session.scalars(select(JobSource).where(JobSource.job_id == job_id)).all()
    preferred = next(
        (
            r
            for r in sorted(rows, key=lambda r: r.availability != "open")
            if r.source_id and r.application_url and r.availability != "closed"
        ),
        None,
    )
    preferred = preferred or next(
        (r for r in rows if r.source_id and r.availability != "closed"), None
    )
    states = {row.availability for row in rows}
    return {
        "availability": "open"
        if "open" in states
        else "closed"
        if states == {"closed"}
        else "unknown",
        "direct_only": bool(any(r.source_id for r in rows))
        and not any(not r.source_id for r in rows),
        "sources": [
            {
                "id": r.id,
                "namespace": r.namespace,
                "posting_id": r.posting_id,
                "variant": r.variant,
                "requisition_id": r.requisition_id,
                "application_url": r.application_url,
                "direct": r.source_id is not None,
                "url": r.url,
                "availability": r.availability,
                "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r in rows
        ],
        "application_url": preferred.application_url if preferred else None,
        "locations": json.loads(preferred.payload_json).get("locations", []) if preferred else [],
        "score_stale": any(r.score_stale for r in rows),
        "content_changed_at": max(
            (r.content_changed_at.isoformat() for r in rows if r.content_changed_at), default=None
        ),
    }


def board_may_enrich(session, job_id):
    """Board detail refreshes cannot replace an authoritative employer description."""
    job = session.get(Job, job_id)
    return (
        job is None
        or job.description_quality != "complete"
        or (
            session.scalar(
                select(JobSource.id).where(
                    JobSource.job_id == job_id, JobSource.source_id.is_not(None)
                )
            )
            is None
        )
    )


def mark_scores_fresh(session, job_id, profile_id=None):
    if profile_id:
        row = session.get(JobProfile, (job_id, profile_id))
        if row:
            row.score_stale = False
    session.flush()
    stale = session.scalar(
        select(JobProfile.job_id).where(
            JobProfile.job_id == job_id, JobProfile.score_stale.is_(True)
        )
    )
    if stale is None:
        session.query(JobSource).filter_by(job_id=job_id).update(
            {JobSource.score_stale: False}, synchronize_session=False
        )
