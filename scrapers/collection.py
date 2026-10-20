"""Shared board ingestion and durable diagnostics for all collection entrypoints."""

import json
from datetime import datetime

from analyzer.freshness import utc_naive
from db.models import BoardSearchRun, Job, RawJob
from db.session import get_session


async def collect_board(
    scraper,
    keyword,
    location,
    max_pages,
    *,
    direction=None,
    profile_ids=None,
    max_age_days=None,
    include_unknown_dates=True,
):
    from analyzer.quality import set_description
    from dedup.exact import get_or_create_job
    from employers.discovery import observe_board_result

    scraper.report = {"requests": 0, "pages": 0, "errors": [], "termination": None}

    with get_session() as session:
        run = BoardSearchRun(
            source=scraper.source_name,
            keyword=keyword,
            location=location,
            profiles_json=json.dumps(profile_ids or []),
        )
        session.add(run)
        session.flush()
        run_id = run.id
    found = retained = created_count = 0
    failure = None
    try:
        async for scraped in scraper.scrape(keyword, location, max_pages):
            # Discovery intentionally precedes every vacancy retention decision.
            observe_board_result(scraped)
            found += 1
            if not scraped.posted_at and not include_unknown_dates:
                continue
            if (
                scraped.posted_at
                and max_age_days is not None
                and (datetime.utcnow() - utc_naive(scraped.posted_at)).total_seconds()
                > max_age_days * 86400
            ):
                continue
            job, created = get_or_create_job(scraped, direction=direction, profile_ids=profile_ids)
            retained += 1
            created_count += created
            with get_session() as session:
                current = session.get(Job, job.id)
                from employers.ingestion import board_may_enrich

                if current and board_may_enrich(session, current.id):
                    set_description(
                        session,
                        current,
                        scraped.description,
                        declared=scraped.extra.get("description_quality"),
                        trusted_detail=scraped.extra.get("description_quality") == "complete",
                        source=scraped.source,
                        evidence=[scraped.url],
                    )
                from analyzer.matching import local_evaluate
                from db.models import SearchProfile

                for pid in profile_ids or []:
                    profile = session.get(SearchProfile, pid)
                    if profile and current:
                        local_evaluate(session, current, profile)
                if (
                    created
                    and not session.query(RawJob.id)
                    .filter_by(source=scraped.source, source_job_id=scraped.source_job_id)
                    .first()
                ):
                    session.add(
                        RawJob(
                            canonical_id=job.id,
                            source=scraped.source,
                            source_job_id=scraped.source_job_id,
                            url=scraped.url,
                            raw_html=scraped.raw_html,
                            raw_json=scraped.raw_json,
                        )
                    )
            yield scraped, job, created
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        report = dict(scraper.report)
        errors = report.get("errors", [])
        outcome = (
            ("partial" if found else "failed")
            if failure or errors
            else (
                "empty"
                if report.get("verified_empty") and not found
                else "complete"
                if report.get("termination") and found
                else "partial"
                if found
                else "failed"
            )
        )
        report.update(
            found=found,
            retained=retained,
            new=created_count,
            requested_pages=max_pages,
            max_age_days=max_age_days,
            include_unknown_dates=include_unknown_dates,
        )
        with get_session() as session:
            run = session.get(BoardSearchRun, run_id)
            run.outcome, run.finished_at, run.stats_json = (
                outcome,
                datetime.utcnow(),
                json.dumps(report),
            )
            run.error = (
                failure
                or "; ".join(errors)[:1000]
                or (
                    "Collector did not provide termination/empty evidence"
                    if outcome in {"failed", "partial"}
                    else None
                )
            )
        scraper.report.update(outcome=outcome, run_id=run_id)
