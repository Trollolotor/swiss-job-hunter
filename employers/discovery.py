"""Evidence-first employer discovery, including resumable historical imports."""

import json
import re
from datetime import datetime

from sqlalchemy import select

from company_service import normalize_company_name
from db.models import Employer, EmployerAlias, EmployerEvidence, Job, RawJob
from db.session import get_session, init_db
from employers.contracts import digest
from scrapers.base import ScrapedJob


def board_metadata(job):
    """Reuse explicit structured board fields; never infer an agency's client."""
    if not job.raw_json or len(job.raw_json) > 5 * 1024 * 1024:
        return
    try:
        value = json.loads(job.raw_json)
    except (ValueError, TypeError):
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("json_ld"), dict):
        value = value["json_ld"]
    organization = value.get("hiringOrganization") or value.get("company") or {}
    if not isinstance(organization, dict):
        organization = {}
    candidates = {
        "company_website": value.get("company_website")
        or organization.get("sameAs")
        or organization.get("url"),
        "application_url": value.get("applyUrl")
        or value.get("applicationUrl")
        or value.get("application_url"),
        "requisition_id": value.get("requisitionId")
        or value.get("requisition_id")
        or value.get("refNumber"),
    }
    for key, entry in candidates.items():
        if isinstance(entry, (str, int)) and str(entry).strip():
            job.extra.setdefault(key, str(entry))
    if organization.get("@type") in {"EmploymentAgency", "RecruitmentAgency"}:
        job.extra.setdefault("employer_classification", "agency")


def observe(session, job: ScrapedJob, *, historical=False, observed_at=None):
    board_metadata(job)
    name = re.sub(r"\s+", " ", job.company).strip()[:300]
    if not name or name.casefold() in {"confidential", "unknown", "anonymous", "n/a"}:
        return None
    key = digest([name.casefold(), job.extra.get("employer_identity", "")])
    employer = session.scalar(select(Employer).where(Employer.identity_key == key))
    if not employer:
        employer = Employer(name=name, identity_key=key)
        session.add(employer)
        session.flush()
        session.add(
            EmployerAlias(
                employer_id=employer.id, name=name, normalized=normalize_company_name(name)
            )
        )
    when = observed_at or datetime.utcnow()
    if job.extra.get("employer_classification") == "agency":
        employer.classification = "agency"
    employer.last_seen_at = max(employer.last_seen_at or when, when)
    ek = digest([employer.id, job.source, job.source_job_id or job.url])
    if not session.scalar(select(EmployerEvidence.id).where(EmployerEvidence.evidence_key == ek)):
        session.add(
            EmployerEvidence(
                employer_id=employer.id,
                evidence_key=ek,
                kind="board",
                url=job.url,
                source=job.source,
                observed_at=when,
                historical=historical,
                payload_json=json.dumps(
                    {
                        "company": name,
                        "posting_id": job.source_job_id,
                        "official_url": job.extra.get("company_website"),
                        "application_url": job.extra.get("application_url"),
                    },
                    ensure_ascii=False,
                ),
            )
        )
    return employer


def observe_board_result(job):
    from employers.config import enabled

    if not enabled():
        return
    init_db()
    with get_session() as session:
        observe(session, job)


def backfill(batch_size=500):
    """Checkpoints and all writes share a transaction. Unknown history stays unknown."""
    from db.models import AppSetting
    from employers.ingestion import ingest

    init_db()
    with get_session() as session:
        setting = session.get(AppSetting, "employer_backfill")
        checkpoint = json.loads(setting.value_json) if setting else {"job_id": 0, "raw_id": 0}
        jobs = session.scalars(
            select(Job).where(Job.id > checkpoint["job_id"]).order_by(Job.id).limit(batch_size)
        ).all()
        for job in jobs:
            scraped = ScrapedJob(
                title=job.title,
                company=job.company,
                location=job.location,
                description=job.description,
                url=job.url,
                source=job.source,
                source_job_id=job.source_job_id,
                posted_at=job.posted_at,
                posted_at_source=job.posted_at_source,
                posted_at_raw=job.posted_at_raw,
                posted_at_confidence=job.posted_at_confidence,
            )
            employer = observe(session, scraped, historical=True, observed_at=job.scraped_at)
            # Some historical rows have neither a posting ID nor a URL. Keep
            # their employer observation, but do not invent a source identity.
            if (scraped.source_job_id or "").strip() or (scraped.url or "").strip():
                ingest(
                    session,
                    scraped,
                    employer_id=employer.id if employer else None,
                    canonical_id=job.id,
                    historical=True,
                    observed_at=job.scraped_at,
                )
            checkpoint["job_id"] = job.id
        raws = session.scalars(
            select(RawJob)
            .where(RawJob.id > checkpoint.get("raw_id", 0))
            .order_by(RawJob.id)
            .limit(batch_size)
        ).all()
        for raw in raws:
            job = session.get(Job, raw.canonical_id) if raw.canonical_id else None
            if job:
                scraped = ScrapedJob(
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    description="",
                    url=raw.url,
                    source=raw.source,
                    source_job_id=raw.source_job_id,
                    raw_json=raw.raw_json,
                )
                ingest(
                    session,
                    scraped,
                    canonical_id=job.id,
                    historical=True,
                    observed_at=raw.scraped_at,
                )
            checkpoint["raw_id"] = raw.id
        if not setting:
            setting = AppSetting(key="employer_backfill", value_json="{}")
            session.add(setting)
        setting.value_json = json.dumps(checkpoint)
        return {
            **checkpoint,
            "processed": len(jobs) + len(raws),
            "done": len(jobs) < batch_size and len(raws) < batch_size,
        }
