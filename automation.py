"""Scheduled end-to-end pipeline for active search profiles."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_

_scheduler = None
_tasks: set[asyncio.Task] = set()
_run_tasks: dict[int, asyncio.Task] = {}


def group_profile_searches(profiles: list[tuple]) -> list[dict]:
    """Collapse identical keyword/location searches while retaining every profile link."""
    groups: dict[tuple[str, str], dict] = {}
    for profile_id, slug, _name, location, keywords, _cv in profiles:
        for keyword in keywords:
            normalized_keyword = " ".join(str(keyword).split()).strip()
            if not normalized_keyword:
                continue
            normalized_location = location or "Switzerland"
            key = (normalized_keyword.casefold(), normalized_location.casefold())
            group = groups.setdefault(key, {
                "keyword": normalized_keyword,
                "location": normalized_location,
                "profiles": [],
            })
            profile = (profile_id, slug)
            if profile not in group["profiles"]:
                group["profiles"].append(profile)
    return list(groups.values())


def _run_dict(row) -> dict:
    try:
        stats = json.loads(row.stats_json or "{}")
    except json.JSONDecodeError:
        stats = {}
    return {"id": row.id, "trigger": row.trigger, "status": row.status,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "stats": stats, "error": row.error}


def list_runs(limit: int = 30) -> list[dict]:
    from db.models import AutomationRun
    from db.session import get_session, init_db
    init_db()
    with get_session() as session:
        return [_run_dict(row) for row in session.query(AutomationRun).order_by(
            AutomationRun.started_at.desc()).limit(limit).all()]


def _acquire(trigger: str) -> int | None:
    from db.models import AutomationRun
    from db.session import engine, get_session, init_db
    from sqlalchemy import text
    init_db()
    now = datetime.utcnow()
    lease_until = now + timedelta(hours=6)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT OR IGNORE INTO automation_lease (id, lease_until, run_id) VALUES (1, NULL, NULL)"
        ))
        acquired = connection.execute(text(
            "UPDATE automation_lease SET lease_until=:lease_until "
            "WHERE id=1 AND (lease_until IS NULL OR lease_until <= :now)"
        ), {"lease_until": lease_until, "now": now})
        if acquired.rowcount != 1:
            return None
    with get_session() as session:
        session.query(AutomationRun).filter(
            AutomationRun.status == "running", AutomationRun.lease_until <= now).update(
                {AutomationRun.status: "failed", AutomationRun.finished_at: now,
                 AutomationRun.error: "stale automation lease"}, synchronize_session=False)
        row = AutomationRun(trigger=trigger, status="running", lease_until=lease_until)
        session.add(row)
        session.flush()
        run_id = row.id
    with engine.begin() as connection:
        connection.execute(text("UPDATE automation_lease SET run_id=:run_id WHERE id=1"),
                           {"run_id": run_id})
    return run_id


async def start_run(trigger: str = "schedule", background: bool = False) -> int | None:
    run_id = _acquire(trigger)
    if run_id is None:
        return None
    if background:
        task = asyncio.create_task(_execute(run_id))
        _tasks.add(task)
        _run_tasks[run_id] = task
        def cleanup(completed):
            _tasks.discard(completed)
            _run_tasks.pop(run_id, None)
        task.add_done_callback(cleanup)
    else:
        await _execute(run_id)
    return run_id


def cancel_run(run_id: int) -> bool:
    task = _run_tasks.get(run_id)
    if not task or task.done():
        return False
    task.cancel()
    return True


async def scheduled_tick() -> None:
    from config.runtime import get_automation_config
    config = get_automation_config()
    if not config.get("enabled"):
        return
    local = datetime.now(ZoneInfo(config["timezone"]))
    if local.weekday() not in config["weekdays"]:
        return
    if not config["start_hour"] <= local.hour < config["end_hour"]:
        return
    await start_run("schedule", background=True)


async def _execute(run_id: int) -> None:
    from analyzer.freshness import utc_naive
    from config.runtime import get_automation_config
    from db.models import Job, JobProfile, JobStatus, SearchProfile
    from db.session import get_session
    from dedup.exact import get_or_create_job
    from scrapers import SCRAPER_REGISTRY

    config = get_automation_config()
    semaphore = asyncio.Semaphore(config.get("concurrency", 5))

    async def bounded(awaitable):
        async with semaphore:
            return await awaitable

    stats = {"profiles": 0, "search_groups": 0, "found": 0, "new": 0,
             "enriched": 0, "screened": 0, "companies": 0,
             "date_backfilled": 0, "availability_checks": 0,
             "date_coverage": {}, "sources": {}, "phase_seconds": {}}
    try:
        run_started = time.monotonic()
        phase_started = run_started
        with get_session() as session:
            profiles = [(p.id, p.slug, p.name, p.default_location,
                         json.loads(p.keywords_json or "[]"), p.cv_text)
                        for p in session.query(SearchProfile).filter_by(active=True).all()]
        stats["profiles"] = len(profiles)
        new_ids: set[int] = set()
        search_groups = group_profile_searches(profiles)
        stats["search_groups"] = len(search_groups)
        with get_session() as session:
            known_jobscout = {
                row.source_job_id: {
                    "title": row.title, "company": row.company, "location": row.location,
                    "description": row.description or "", "url": row.url,
                    "posted_at": row.posted_at, "posted_at_source": row.posted_at_source,
                    "posted_at_raw": row.posted_at_raw,
                    "posted_at_confidence": row.posted_at_confidence or 0.0,
                }
                for row in session.query(Job).filter(
                    Job.source == "jobscout24.ch", Job.source_job_id.isnot(None)
                ).all()
            }
        async def search_source(source: str) -> None:
            source_stats = stats["sources"].setdefault(
                source, {"found": 0, "new": 0, "errors": 0}
            )
            scraper_cls = SCRAPER_REGISTRY.get(source)
            if not scraper_cls:
                source_stats["errors"] += 1
                return
            kwargs = {
                "time_range": f"r{config['max_age_days'] * 86400}"
            } if source == "linkedin.com" else {}
            if source == "jobscout24.ch":
                kwargs["known_jobs"] = known_jobscout
            try:
                async with scraper_cls(**kwargs) as scraper:
                    for group in search_groups:
                        keyword, location = group["keyword"], group["location"]
                        async for scraped in scraper.scrape(
                            keyword, location, config["pages_per_source"]
                        ):
                            stats["found"] += 1
                            source_stats["found"] += 1
                            if not scraped.posted_at and not config.get(
                                "include_unknown_dates", True
                            ):
                                continue
                            if scraped.posted_at:
                                age = datetime.utcnow() - utc_naive(scraped.posted_at)
                                if age.total_seconds() > config["max_age_days"] * 86400:
                                    continue
                            _first_profile_id, first_slug = group["profiles"][0]
                            job, created = get_or_create_job(
                                scraped,
                                direction=first_slug,
                                profile_ids=[profile_id for profile_id, _slug in group["profiles"]],
                            )
                            if created:
                                new_ids.add(job.id)
                                stats["new"] += 1
                                source_stats["new"] += 1
            except Exception:
                source_stats["errors"] += 1
            finally:
                _update_stats(run_id, stats)

        await asyncio.gather(*[
            search_source(source) for source in config["sources"]
        ])
        stats["phase_seconds"]["search"] = round(time.monotonic() - phase_started, 2)
        _update_stats(run_id, stats)
        if config.get("enrich"):
            phase_started = time.monotonic()
            enrich_limit = config.get("enrich_limit", 100)
            with get_session() as session:
                enrich_ids = [row[0] for row in session.query(Job.id).filter(
                    Job.status != JobStatus.ARCHIVED,
                    or_(Job.description.is_(None), Job.description == "", func.length(Job.description) < 100),
                ).order_by(Job.last_seen_at.desc()).limit(enrich_limit).all()]
            enriched = await asyncio.gather(*[
                bounded(_enrich_or_check(job_id, availability=False)) for job_id in enrich_ids
            ]) if enrich_ids else []
            stats["enriched"] = sum(bool(value) for value in enriched)
            stats["phase_seconds"]["enrich"] = round(time.monotonic() - phase_started, 2)
            _update_stats(run_id, stats)
        date_limit = config.get("date_backfill_limit", 20)
        if date_limit:
            phase_started = time.monotonic()
            with get_session() as session:
                query = session.query(Job.id).filter(
                    Job.posted_at.is_(None), Job.status != JobStatus.ARCHIVED,
                )
                if config.get("enrich") and enrich_ids:
                    query = query.filter(~Job.id.in_(enrich_ids))
                date_ids = [row[0] for row in query.order_by(
                    Job.last_seen_at.desc()).limit(date_limit).all()]
            before = len(date_ids)
            if date_ids:
                await asyncio.gather(*[
                    bounded(_enrich_or_check(job_id, availability=False)) for job_id in date_ids
                ])
            with get_session() as session:
                remaining = session.query(func.count(Job.id)).filter(
                    Job.id.in_(date_ids), Job.posted_at.is_(None)
                ).scalar() if date_ids else 0
            stats["date_backfilled"] = before - int(remaining or 0)
            stats["phase_seconds"]["date_backfill"] = round(
                time.monotonic() - phase_started, 2
            )
            _update_stats(run_id, stats)
        if config.get("screen"):
            from analyzer.scorer import llm_score
            phase_started = time.monotonic()
            screen_limit = config.get("screen_limit_per_profile", 20)
            for profile_id, _slug, name, _location, _keywords, cv_text in profiles:
                with get_session() as session:
                    rows = session.query(Job, JobProfile).join(
                        JobProfile, JobProfile.job_id == Job.id
                    ).filter(
                        JobProfile.profile_id == profile_id,
                        JobProfile.match_score.is_(None),
                        Job.status != JobStatus.ARCHIVED,
                        func.length(Job.description) >= 100,
                    ).order_by(Job.last_seen_at.desc()).limit(screen_limit).all()
                    payloads = [(j.id, j.title, j.description or "") for j, _link in rows]
                async def screen_one(job_id, title, description):
                    if len(description) < 100:
                        return False
                    try:
                        result = await bounded(llm_score(cv_text, title, description, role=name))
                        with get_session() as session:
                            job = session.get(Job, job_id)
                            if job:
                                job.match_score, job.match_explanation = result.score, result.explanation
                                job.status = JobStatus.SHORTLISTED if result.score >= .3 else JobStatus.ANALYZED
                                link = session.get(JobProfile, (job_id, profile_id))
                                if link:
                                    link.match_score, link.match_explanation = result.score, result.explanation
                                    link.screened_at = datetime.utcnow()
                        return True
                    except Exception:
                        return False
                screened = await asyncio.gather(*[screen_one(*payload) for payload in payloads]) if payloads else []
                stats["screened"] += sum(screened)
            stats["phase_seconds"]["screen"] = round(time.monotonic() - phase_started, 2)
            _update_stats(run_id, stats)
        if config.get("company_enrichment"):
            from company_service import enrich_company, normalize_company_name
            from db.models import CompanyInfo
            phase_started = time.monotonic()
            with get_session() as session:
                names = [row[0] for row in session.query(
                    Job.company, func.max(Job.last_seen_at)
                ).filter(Job.status != JobStatus.ARCHIVED).group_by(
                    Job.company
                ).order_by(func.max(Job.last_seen_at).desc()).all() if row[0]]
                cached = {r[0] for r in session.query(CompanyInfo.normalized_name).filter(
                    CompanyInfo.summary.isnot(None), CompanyInfo.industry.isnot(None)).all() if r[0]}
            names = [name for name in names if normalize_company_name(name) not in cached][
                :config.get("company_limit", 10)
            ]
            async def company_one(name):
                if normalize_company_name(name) in cached:
                    return False
                try:
                    await bounded(enrich_company(name))
                    return True
                except Exception:
                    return False
            enriched_companies = await asyncio.gather(*[company_one(name) for name in names]) if names else []
            stats["companies"] = sum(enriched_companies)
            stats["phase_seconds"]["companies"] = round(time.monotonic() - phase_started, 2)
            _update_stats(run_id, stats)
        limit = config.get("availability_limit", 0)
        if limit:
            phase_started = time.monotonic()
            with get_session() as session:
                candidates = [r[0] for r in session.query(Job.id).filter(
                    Job.status != JobStatus.ARCHIVED,
                    or_(Job.availability_checked_at.is_(None),
                        Job.availability_checked_at < datetime.utcnow() - timedelta(
                            hours=config.get("availability_interval_hours", 24)
                        )),
                ).order_by(Job.availability_checked_at.asc()).limit(limit).all()]
            if candidates:
                await asyncio.gather(*[bounded(_enrich_or_check(job_id, availability=True)) for job_id in candidates])
            stats["availability_checks"] = len(candidates)
            stats["phase_seconds"]["availability"] = round(
                time.monotonic() - phase_started, 2
            )
        stats["phase_seconds"]["total"] = round(time.monotonic() - run_started, 2)
        with get_session() as session:
            rows = session.query(Job.source, Job.posted_at).all()
        coverage: dict[str, list[int]] = {}
        for source, posted in rows:
            values = coverage.setdefault(source, [0, 0])
            values[1] += 1
            if posted:
                values[0] += 1
        stats["date_coverage"] = {k: {"known": v[0], "total": v[1],
                                               "percent": round(v[0] / v[1] * 100, 1) if v[1] else 0}
                                  for k, v in coverage.items()}
        _finish(run_id, "completed", stats)
    except asyncio.CancelledError:
        _finish(run_id, "failed", stats, "automation run cancelled during shutdown")
        raise
    except Exception as exc:
        _finish(run_id, "failed", stats, f"{type(exc).__name__}: {exc}"[:1000])


def _update_stats(run_id: int, stats: dict) -> None:
    from db.models import AutomationRun
    from db.session import get_session
    with get_session() as session:
        row = session.get(AutomationRun, run_id)
        if row:
            row.stats_json = json.dumps(stats, ensure_ascii=False)


async def _enrich_or_check(job_id: int, availability: bool) -> bool:
    import importlib
    from analyzer.publication import extract_publication_from_html
    from db.models import Job
    from db.session import get_session
    scraper_map = {
        "jobs.ch": "scrapers.jobs_ch.JobsChScraper", "jobscout24.ch": "scrapers.jobscout24.JobScout24Scraper",
        "swissdevjobs.ch": "scrapers.swissdevjobs.SwissDevJobsScraper", "züri.jobs": "scrapers.zuri_jobs.ZuriJobsScraper",
        "efinancialcareers.ch": "scrapers.efinancialcareers.EFinancialCareersScraper", "jobup.ch": "scrapers.jobup_ch.JobupChScraper",
        "linkedin.com": "scrapers.linkedin_rss.LinkedInRssScraper", "michael-page.ch": "scrapers.michael_page.MichaelPageScraper",
        "indeed.ch": "scrapers.indeed_ch.IndeedChScraper",
    }
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job or job.source not in scraper_map:
            return False
        source, identifier, url = job.source, job.source_job_id or job.url, job.url
        needs_date = not bool(job.posted_at)
        if identifier and identifier.isdigit():
            identifier = url
    path = scraper_map[source]
    module, cls = path.rsplit(".", 1)
    try:
        async with getattr(importlib.import_module(module), cls)() as scraper:
            fetcher = getattr(scraper, "fetch_full_description", None)
            result = await fetcher(identifier) if fetcher else None
            published = None
            if needs_date:
                try:
                    response = await scraper._fetch(url)
                    published = extract_publication_from_html(response.text)
                except Exception:
                    pass
    except Exception:
        return False
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return False
        job.availability_checked_at = datetime.utcnow()
        if result == ():
            from job_lifecycle import mark_unavailable
            mark_unavailable(job, "source returned 404/410 or explicit closure")
            return False
        if result:
            from job_lifecycle import mark_available
            mark_available(job)
            if len(result[0]) > 100:
                job.description = result[0]
            if result[1]:
                job.url = result[1]
        if published and published.value and (not job.posted_at or published.confidence > (job.posted_at_confidence or 0)):
            job.posted_at, job.posted_at_source = published.value, published.source
            job.posted_at_raw, job.posted_at_confidence = published.raw, published.confidence
    return bool(result)


def _finish(run_id: int, status: str, stats: dict, error: str | None = None) -> None:
    from db.models import AutomationRun
    from db.session import engine, get_session
    from sqlalchemy import text
    with get_session() as session:
        row = session.get(AutomationRun, run_id)
        if row:
            row.status, row.finished_at, row.lease_until = status, datetime.utcnow(), None
            row.stats_json, row.error = json.dumps(stats, ensure_ascii=False), error
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE automation_lease SET lease_until=NULL, run_id=NULL WHERE id=1 AND run_id=:run_id"
        ), {"run_id": run_id})


def start_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    _scheduler = AsyncIOScheduler(timezone="Europe/Zurich")
    _scheduler.start()
    reschedule()


def reschedule() -> None:
    if not _scheduler:
        raise RuntimeError("scheduler has not started")
    from config.runtime import get_automation_config
    config = get_automation_config()
    _scheduler.add_job(scheduled_tick, "interval", minutes=config["interval_minutes"],
                       id="job-search", replace_existing=True, max_instances=1, coalesce=True)


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
    _scheduler = None
