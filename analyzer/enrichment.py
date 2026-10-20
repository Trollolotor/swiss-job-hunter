"""Evidence-based description recovery and the shared availability mechanism."""

from db.models import Job
from db.session import get_session


async def enrich(job_id, *, availability=False):
    from analyzer.publication import extract_publication_from_html
    from analyzer.quality import (
        detail_is_complete,
        extraction_failed,
        needs_enrichment,
        set_description,
    )
    from employers.ingestion import board_may_enrich
    from job_lifecycle import mark_available, mark_unavailable
    from scrapers import SCRAPER_REGISTRY

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return False
        if not availability and not needs_enrichment(job) and job.posted_at:
            return False
        cls = SCRAPER_REGISTRY.get(job.source)
        identifier, url = job.source_job_id or job.url, job.url
        if identifier and identifier.isdigit():
            identifier = url
    if not cls or not hasattr(cls, "fetch_full_description"):
        return False
    result, response, reason, missing = None, None, None, False
    try:
        async with cls() as scraper:
            result = await scraper.fetch_full_description(identifier)
            response = getattr(scraper, "last_response", None)
            missing = result == () or (response is not None and response.status_code in {404, 410})
            if not result and not missing:
                reason = (
                    "; ".join(scraper.report["errors"])
                    or "Detail extraction did not return a description"
                )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return False
        if missing:
            mark_unavailable(job, "Detail endpoint confirmed 404/410 or explicit closure")
            return False
        if reason:
            extraction_failed(job, reason)
            return False
        if result and len(result) == 2 and result[0].strip():
            desc, canonical = result
            complete = detail_is_complete(response.text if response is not None else "", desc)
            if board_may_enrich(session, job.id):
                set_description(
                    session, job, desc, trusted_detail=complete, source=job.source, evidence=[url]
                )
            if canonical:
                from urllib.parse import urlsplit

                if urlsplit(canonical).hostname == urlsplit(url).hostname:
                    job.url = canonical
            mark_available(job)
            if response is not None:
                published = extract_publication_from_html(response.text)
                if published.value and (
                    not job.posted_at or published.confidence > (job.posted_at_confidence or 0)
                ):
                    job.posted_at, job.posted_at_source = published.value, published.source
                    job.posted_at_raw, job.posted_at_confidence = (
                        published.raw,
                        published.confidence,
                    )
            if not complete:
                extraction_failed(
                    job, "Description returned without complete detail-field evidence"
                )
            return complete
        extraction_failed(job, "Empty detail extraction")
        return False
