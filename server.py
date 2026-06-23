"""
UI backend server — FastAPI + SSE streaming.
Run: python server.py
Then open: http://localhost:5173 (after `npm run dev` in ui/)
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func

from config.settings import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from db.session import init_db
    from llm.prompt_manager import seed_prompts
    from automation import start_scheduler, stop_scheduler
    init_db()
    seed_prompts()
    start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler()

app = FastAPI(title="Swiss Job Hunter API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

from api_ext import router as settings_router
app.include_router(settings_router)


@app.get("/directions")
def get_directions():
    import glob
    from config.settings import settings
    pattern = str(settings.cv_text_path.parent / "cv_*.txt")
    dirs = sorted(
        Path(p).stem[3:]  # strip leading "cv_"
        for p in glob.glob(pattern)
    )
    return dirs


@app.get("/config")
def get_config():
    from config.settings import Settings
    s = Settings()
    return {
        "default_keyword": s.default_keyword,
        "default_location": s.default_location,
        "keyword_presets": s.keyword_presets,
    }


@app.get("/presets")
def get_presets():
    """Return keyword presets configured via KEYWORD_PRESETS in .env."""
    from config.settings import Settings
    return Settings().keyword_presets

# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_jobs_query(
    status: str = "all", q: str = "", direction: str = "all", min_stars: int = 0,
    profile_id: Optional[int] = None, published_within: Optional[int] = None,
    sort: str = "priority", include_unknown_dates: bool = True,
):
    from analyzer.freshness import freshness, priority, utc_naive
    from config.runtime import get_priority_config
    from db.session import get_session
    from db.models import Job, SearchProfile, CompanyInfo, JobProfile
    from company_service import company_dict, normalize_company_name
    from sqlalchemy import or_

    config = get_priority_config()
    with get_session() as session:
        query = session.query(Job)
        if status != "all":
            query = query.filter(Job.status == status)
        if profile_id:
            query = query.filter(Job.profiles.any(SearchProfile.id == profile_id))
        elif direction != "all":
            query = query.filter(or_(
                Job.direction == direction,
                Job.profiles.any(SearchProfile.slug == direction),
            ))
        if q:
            query = query.filter(or_(
                Job.title.ilike(f"%{q}%"), Job.company.ilike(f"%{q}%"),
                Job.location.ilike(f"%{q}%"),
            ))
        if min_stars:
            query = query.filter(Job.user_stars >= min_stars)
        jobs = query.all()
        profile_matches = {}
        if profile_id:
            profile_matches = {row.job_id: row for row in session.query(JobProfile).filter_by(
                profile_id=profile_id).all()}
        company_rows = session.query(CompanyInfo).all()
        companies = {row.normalized_name or normalize_company_name(row.name): company_dict(row)
                     for row in company_rows}
        records = []
        for j in jobs:
            fresh_score, age_label, age_days = freshness(j.posted_at, config)
            if published_within is not None:
                if age_days is None and not include_unknown_dates:
                    continue
                if age_days is not None and age_days > published_within:
                    continue
            profile_match = profile_matches.get(j.id)
            match_score = profile_match.match_score if profile_match and profile_match.match_score is not None else j.match_score
            match_explanation = profile_match.match_explanation if profile_match and profile_match.match_explanation else j.match_explanation
            records.append({
                "id": j.id, "title": j.title, "company": j.company,
                "location": j.location, "description": j.description, "url": j.url,
                "source": j.source, "source_job_id": j.source_job_id,
                "salary_raw": j.salary_raw, "employment_type": j.employment_type,
                "status": j.status, "match_score": match_score,
                "match_explanation": match_explanation, "user_stars": j.user_stars,
                "direction": j.direction,
                "company_info": companies.get(normalize_company_name(j.company)),
                "profiles": [{"id": p.id, "slug": p.slug, "name": p.name} for p in j.profiles],
                "posted_at": j.posted_at.isoformat() if j.posted_at else None,
                "posted_at_source": j.posted_at_source or "unknown",
                "posted_at_raw": j.posted_at_raw,
                "posted_at_confidence": j.posted_at_confidence or 0.0,
                "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
                "last_seen_at": j.last_seen_at.isoformat() if j.last_seen_at else None,
                "availability_checked_at": j.availability_checked_at.isoformat() if j.availability_checked_at else None,
                "unavailable_checks": j.unavailable_checks or 0,
                "expired_at": j.expired_at.isoformat() if j.expired_at else None,
                "expired_reason": j.expired_reason,
                "freshness_score": fresh_score, "age_label": age_label,
                "age_days": age_days,
                "priority_score": priority(match_score, fresh_score, config),
                "priority_match_weight": config["match_weight"],
                "priority_freshness_weight": config["freshness_weight"],
            })
    def timestamp(record, field):
        value = record.get(field)
        return utc_naive(datetime.fromisoformat(value)).timestamp() if value else -1
    secondary = {
        "newest": lambda r: timestamp(r, "posted_at"),
        "match": lambda r: r["match_score"] if r["match_score"] is not None else -1,
        "recently_found": lambda r: timestamp(r, "scraped_at"),
        "priority": lambda r: r["priority_score"],
    }.get(sort, lambda r: r["priority_score"])
    records.sort(key=lambda r: (r["user_stars"] or 0, secondary(r)), reverse=True)
    return records


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/jobs")
def list_jobs(
    status: str = "all", q: str = "", direction: str = "all", min_stars: int = 0,
    profile_id: Optional[int] = None, published_within: str = "",
    sort: str = "priority", include_unknown_dates: bool = True,
):
    from db.session import init_db
    init_db()
    days = int(published_within) if published_within.strip() else None
    return get_jobs_query(status, q, direction, min_stars, profile_id, days, sort,
                          include_unknown_dates)


@app.get("/stats")
def get_stats(threshold: float = 0.1):
    from db.session import get_session, init_db
    from db.models import Job
    init_db()
    with get_session() as session:
        total = session.query(func.count(Job.id)).scalar() or 0
        by_status = dict(
            session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
        )
        by_source = dict(
            session.query(Job.source, func.count(Job.id)).group_by(Job.source).all()
        )
        avg_score = session.query(func.avg(Job.match_score)).filter(
            Job.match_score.isnot(None)
        ).scalar()
        top_score = session.query(func.max(Job.match_score)).scalar()
        above_threshold = session.query(func.count(Job.id)).filter(
            Job.match_score >= threshold
        ).scalar() or 0

    return {
        "total": total,
        "by_status": by_status,
        "by_source": by_source,
        "avg_score": float(avg_score) if avg_score else None,
        "top_score": float(top_score) if top_score else None,
        "above_threshold": above_threshold,
        "threshold": threshold,
    }


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int):
    from db.session import get_session
    from db.models import Job, RawJob, Application, JobEvent
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        session.query(JobEvent).filter(JobEvent.job_id == job_id).delete()
        session.query(Application).filter(Application.job_id == job_id).delete()
        session.query(RawJob).filter(RawJob.canonical_id == job_id).delete()
        session.delete(job)
    return {"ok": True}


@app.patch("/jobs/{job_id}/stars")
def update_stars(job_id: int, body: dict):
    from db.session import get_session
    from db.models import Job
    stars = body.get("stars")
    if stars is not None and stars not in (0, 1, 2, 3, 4, 5):
        raise HTTPException(400, "stars must be 0-5 (0 = clear)")
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job.user_stars = None if stars == 0 else stars
    return {"ok": True}


@app.patch("/jobs/{job_id}/status")
def update_status(job_id: int, body: dict):
    from db.session import get_session
    from db.models import Job, JobStatus
    new_status = body.get("status")
    try:
        s = JobStatus(new_status)
    except ValueError:
        raise HTTPException(400, f"Invalid status: {new_status}")
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job.status = s
    return {"ok": True}


# ── SSE streaming commands ─────────────────────────────────────────────────────

async def sse(gen: AsyncGenerator[str, None]) -> StreamingResponse:
    async def wrapper():
        try:
            async for line in gen:
                safe = line.replace("\n", " ")
                yield f"data: {safe}\n\n"
        except Exception as e:
            yield f"data: ✗ Internal error: {str(e)[:200]}\n\n"
        finally:
            yield "data: [DONE]\n\n"
    return StreamingResponse(
        wrapper(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


class SearchRequest(BaseModel):
    keyword: str = "ML engineer"
    keywords: list[str] = []  # if non-empty, overrides keyword; each is searched in turn
    location: str = "Zürich"
    sources: list[str] = ["jobs.ch"]
    pages: int = 3
    semantic: bool = False
    direction: Optional[str] = None
    profile_id: Optional[int] = None
    max_age_days: Optional[int] = None
    include_unknown_dates: bool = True
    linkedin_time_range: str = "r604800"  # r86400=24h | r604800=7d | r2592000=30d
    linkedin_experience_level: str = "3,4"  # 2=Entry,3=Associate,4=Senior,5=Director


@app.post("/run/search")
async def run_search(req: SearchRequest):
    async def gen():
        from scrapers import SCRAPER_REGISTRY
        from dedup.exact import get_or_create_job, is_exact_duplicate
        from db import init_db
        from db.models import RawJob
        from db.session import get_session
        init_db()
        active_direction = req.direction
        profile_keywords: list[str] = []
        if req.profile_id:
            from db.models import SearchProfile
            with get_session() as session:
                profile = session.get(SearchProfile, req.profile_id)
                if not profile:
                    yield "✗ Search profile not found"
                    return
                active_direction = profile.slug
                try:
                    profile_keywords = json.loads(profile.keywords_json)
                except json.JSONDecodeError:
                    profile_keywords = []


        kw_list = req.keywords if req.keywords else [req.keyword]
        total_new = 0
        linkedin_in_sources = "linkedin.com" in req.sources

        for kw_idx, kw in enumerate(kw_list):
            if kw_idx > 0 and linkedin_in_sources:
                yield f"⏳ LinkedIn cooldown 5s..."
                await asyncio.sleep(5)
            yield f"─── keyword: {kw} · {req.location or 'Switzerland (all)'}"

            for source_name in req.sources:
                scraper_cls = SCRAPER_REGISTRY.get(source_name)
                if not scraper_cls:
                    yield f"✗ Unknown source: {source_name}"
                    continue

                yield f"→ {source_name}"
                new_count = 0
                found_count = 0
                try:
                    kwargs = {}
                    if source_name == "linkedin.com":
                        kwargs["time_range"] = req.linkedin_time_range
                        kwargs["experience_level"] = req.linkedin_experience_level
                    async with scraper_cls(**kwargs) as scraper:
                        async for scraped in scraper.scrape(kw, req.location, req.pages):
                            found_count += 1
                            if req.max_age_days is not None:
                                if not scraped.posted_at and not req.include_unknown_dates:
                                    continue
                                if scraped.posted_at:
                                    from analyzer.freshness import utc_naive
                                    age = datetime.utcnow() - utc_naive(scraped.posted_at)
                                    if age.total_seconds() > req.max_age_days * 86400:
                                        continue
                            if found_count % 10 == 0:
                                yield f"  ↳ {source_name}: {found_count} fetched so far..."
                            try:
                                job, created = get_or_create_job(scraped, direction=active_direction or None)
                                if created:
                                    try:
                                        with get_session() as session:
                                            raw = RawJob(
                                                canonical_id=job.id,
                                                source=scraped.source,
                                                source_job_id=scraped.source_job_id,
                                                url=scraped.url,
                                                raw_html=scraped.raw_html,
                                                raw_json=scraped.raw_json,
                                            )
                                            session.add(raw)
                                    except Exception:
                                        pass
                                    new_count += 1
                                    yield f"  + [{source_name}] {scraped.title[:50]} @ {scraped.company}"
                            except Exception as e:
                                yield f"  ✗ skipped one job: {str(e)[:80]}"
                                continue
                except Exception as e:
                    total_new += new_count
                    partial = f", saved {new_count} before failure" if new_count else ""
                    yield f"✗ {source_name} failed{partial}: {str(e)[:120]}"
                    continue

                if found_count == 0:
                    yield f"✓ {source_name}: +0 new jobs (scraper returned 0 results)"
                elif new_count == 0:
                    yield f"✓ {source_name}: +0 new jobs ({found_count} found, all duplicates)"
                else:
                    yield f"✓ {source_name}: +{new_count} new jobs"
                total_new += new_count

        yield f"✓ Done — {total_new} total new jobs"
    return await sse(gen())


class EnrichRequest(BaseModel):
    limit: int = 50
    source: str = "jobs.ch"
    rescore_llm: bool = False
    direction: Optional[str] = None
    check_availability: bool = False


@app.post("/run/enrich")
async def run_enrich(req: EnrichRequest):
    async def gen():
        from db.models import Job
        from db.session import get_session
        with get_session() as session:
            query = session.query(Job).filter(Job.source == req.source)
            if not req.check_availability:
                query = query.filter(
                    (Job.description == None) | (Job.description == "") |  # noqa: E711
                    (func.length(Job.description) < 100)
                )
            jobs = query.order_by(Job.scraped_at.desc()).limit(req.limit).all()
            import re as _re
            job_data = []
            for j in jobs:
                dlen = len(j.description or "")
                # Resolve the identifier to pass to fetch_full_description:
                # - UUID source_job_id → pass as-is (jobs.ch style)
                # - URL source_job_id → pass as-is (züri.jobs new records)
                # - purely numeric source_job_id → substitute j.url (züri.jobs old,
                #   efinancialcareers, linkedin store numeric IDs that need the full URL)
                # - slug source_job_id → pass as-is (swissdevjobs)
                # - no source_job_id → extract UUID from URL, else use URL
                sjid = j.source_job_id
                _uuid_re = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                if sjid and sjid.isdigit() and j.url:
                    sjid = j.url
                elif not sjid and j.url:
                    m = _re.search(r'/detail/([a-f0-9-]{36})', j.url)
                    sjid = m.group(1) if m else j.url
                if sjid:
                    job_data.append((j.id, sjid, dlen, j.url))

        to_enrich = [(jid, sjid, url) for jid, sjid, dlen, url in job_data
                     if dlen < 100 or req.check_availability]
        yield f"Enriching {len(to_enrich)} jobs from {req.source}..."

        # Generic enrich — works for any scraper that implements fetch_full_description
        scraper_map = {
            "jobs.ch": "scrapers.jobs_ch.JobsChScraper",
            "jobscout24.ch": "scrapers.jobscout24.JobScout24Scraper",
            "swissdevjobs.ch": "scrapers.swissdevjobs.SwissDevJobsScraper",
            "züri.jobs": "scrapers.zuri_jobs.ZuriJobsScraper",
            "efinancialcareers.ch": "scrapers.efinancialcareers.EFinancialCareersScraper",
            "jobup.ch": "scrapers.jobup_ch.JobupChScraper",
            "linkedin.com": "scrapers.linkedin_rss.LinkedInRssScraper",
            "michael-page.ch": "scrapers.michael_page.MichaelPageScraper",
        }

        scraper_path = scraper_map.get(req.source)
        if not scraper_path:
            yield f"– Enrich not yet implemented for {req.source}"
            return

        module_path, cls_name = scraper_path.rsplit(".", 1)
        import importlib
        module = importlib.import_module(module_path)
        scraper_cls = getattr(module, cls_name)

        updated = 0
        enriched_ids = []
        try:
            async with scraper_cls() as scraper:
                for job_id, source_job_id, job_url in to_enrich:
                    try:
                        result = await scraper.fetch_full_description(source_job_id)
                        if result and len(result) == 2 and len(result[0]) > 100:
                            desc, canonical_url = result
                            with get_session() as session:
                                job = session.get(Job, job_id)
                                if job:
                                    job.description = desc
                                    if canonical_url:
                                        job.url = canonical_url
                                    from job_lifecycle import mark_available
                                    mark_available(job)
                            try:
                                from analyzer.publication import extract_publication_from_html
                                detail_response = await scraper._fetch(canonical_url or job_url)
                                published = extract_publication_from_html(detail_response.text)
                                if published.value:
                                    with get_session() as session:
                                        job = session.get(Job, job_id)
                                        if job and (not job.posted_at or published.confidence > (job.posted_at_confidence or 0)):
                                            job.posted_at = published.value
                                            job.posted_at_source = published.source
                                            job.posted_at_raw = published.raw
                                            job.posted_at_confidence = published.confidence
                            except Exception:
                                pass
                            updated += 1
                            enriched_ids.append(job_id)
                            yield f"✓ job #{job_id} — {len(desc)} chars"
                        elif result == ():
                            with get_session() as session:
                                job = session.get(Job, job_id)
                                if job:
                                    from job_lifecycle import mark_unavailable
                                    mark_unavailable(job, "source returned 404/410 or explicit closure")
                            yield f"– job #{job_id} — unavailable confirmation recorded"
                        else:
                            yield f"– job #{job_id} — no detail available"
                    except Exception as e:
                        yield f"✗ job #{job_id} error: {str(e)[:80]}"
        except Exception as e:
            yield f"✗ Enrich failed: {str(e)[:120]}"
        yield f"✓ Enriched {updated}/{len(to_enrich)} jobs"

        if req.rescore_llm and enriched_ids:
            from analyzer.scorer import llm_score, load_cv_text
            from db.models import JobStatus
            yield f"→ LLM scoring {len(enriched_ids)} newly enriched jobs..."
            try:
                cv_text = load_cv_text(direction=req.direction or None)
            except FileNotFoundError as e:
                yield f"✗ CV not found: {e}"
                return
            scored = 0
            for job_id in enriched_ids:
                try:
                    with get_session() as session:
                        job = session.get(Job, job_id)
                        if not job:
                            continue
                        if job.match_score is not None:
                            continue  # already scored, skip
                        title, desc = job.title, job.description or ""
                    result = await llm_score(cv_text, title, desc, role=req.direction or "General")
                    with get_session() as session:
                        job = session.get(Job, job_id)
                        if job:
                            job.match_score = result.score
                            if result.score < 0.1:
                                job.status = JobStatus.ARCHIVED
                            elif result.score >= 0.6:
                                job.status = JobStatus.SHORTLISTED
                    scored += 1
                    yield f"  🧠 job #{job_id} — {round(result.score * 100)}%"
                except Exception as e:
                    yield f"  ✗ job #{job_id} score error: {str(e)[:80]}"
            yield f"✓ LLM scored {scored}/{len(enriched_ids)} jobs"
    return await sse(gen())


class AnalyzeRequest(BaseModel):
    limit: int = 100
    llm: bool = False
    min_score: float = 0.3
    skip_scored: bool = True
    archive_below: float = 0.1  # auto-archive jobs scoring below this (LLM mode only)
    min_keyword_score: float = 0.05  # skip LLM if keyword pre-filter score < this
    direction: Optional[str] = None
    concurrency: int = 10


@app.post("/run/analyze")
async def run_analyze(req: AnalyzeRequest):
    async def gen():
        import asyncio
        from asyncio import Queue
        from analyzer.scorer import fast_score, llm_score, load_cv_text, load_cv_keywords
        from db.models import Job, JobProfile, JobStatus, SearchProfile
        from db.session import get_session
        from sqlalchemy import or_

        try:
            cv_text = load_cv_text(direction=req.direction or None)
        except FileNotFoundError as e:
            yield f"✗ {e}"
            return

        with get_session() as session:
            scoring_profile = session.query(SearchProfile).filter_by(slug=req.direction).first() if req.direction else None
            scoring_profile_id = scoring_profile.id if scoring_profile else None
            statuses = list(JobStatus)  # all statuses when rescoring
            if req.skip_scored:
                statuses = [JobStatus.NEW, JobStatus.ANALYZED, JobStatus.SHORTLISTED, JobStatus.VIEWED, JobStatus.CONSIDERING]
            query = session.query(Job).filter(Job.status.in_(statuses))
            if req.direction:
                query = query.filter(or_(Job.direction == req.direction, Job.profiles.any(SearchProfile.slug == req.direction)))
            if req.skip_scored:
                query = query.filter(Job.match_score.is_(None))
            lim = req.limit if req.skip_scored else 9999
            jobs = query.order_by(Job.scraped_at.desc()).limit(lim).all()
            job_data = [(j.id, j.title, j.description) for j in jobs]

        threshold = req.min_score if not req.llm else min(req.min_score, 0.2)
        yield f"Analyzing {len(job_data)} jobs (mode: {'LLM' if req.llm else 'keyword'}, concurrency: {req.concurrency if req.llm else 1})..."
        if not job_data:
            yield "✓ Nothing to score"
            return
        shortlisted = 0

        if req.llm:
            # Load dynamic CV keywords once for pre-filter (cached per CV file)
            yield f"→ Loading CV keywords for pre-filter..."
            cv_keywords = await load_cv_keywords(cv_text, direction=req.direction or None)
            yield f"→ Loaded {len(cv_keywords)} keywords, pre-filter threshold: {req.min_keyword_score:.0%}"

            queue: Queue = Queue()
            sem = asyncio.Semaphore(req.concurrency)
            completed = 0
            skipped = 0
            total = len(job_data)

            async def score_one(job_id: int, title: str, description: str) -> None:
                nonlocal shortlisted, completed, skipped
                async with sem:
                    try:
                        # Keyword pre-filter: skip LLM if clearly irrelevant
                        kw_result = fast_score(cv_text, description or "", compiled=cv_keywords)
                        if kw_result.score < req.min_keyword_score:
                            with get_session() as session:
                                job = session.get(Job, job_id)
                                if job:
                                    job.match_score = kw_result.score
                                    job.match_explanation = f"[keyword pre-filter] {kw_result.explanation}"
                                    job.status = JobStatus.ARCHIVED
                                    link = session.get(JobProfile, (job_id, scoring_profile_id)) if scoring_profile_id else None
                                    if link:
                                        link.match_score, link.match_explanation = job.match_score, job.match_explanation
                                        link.screened_at = datetime.utcnow()
                            skipped += 1
                            await queue.put(f"– #{job_id} {kw_result.score:.0%} (skipped) — {title[:45]}")
                        else:
                            result = await llm_score(cv_text, title, description or "", role=req.direction or "General")
                            with get_session() as session:
                                job = session.get(Job, job_id)
                                if job:
                                    job.match_score = result.score
                                    job.match_explanation = result.explanation
                                    link = session.get(JobProfile, (job_id, scoring_profile_id)) if scoring_profile_id else None
                                    if link:
                                        link.match_score, link.match_explanation = result.score, result.explanation
                                        link.screened_at = datetime.utcnow()
                                    if result.score >= threshold:
                                        job.status = JobStatus.SHORTLISTED
                                        shortlisted += 1
                                    elif result.score < req.archive_below:
                                        job.status = JobStatus.ARCHIVED
                                    else:
                                        job.status = JobStatus.ANALYZED
                            score_pct = f"{result.score:.0%}"
                            icon = "⭐" if result.score >= req.min_score else ("✗" if result.score < req.archive_below else "·")
                            await queue.put(f"{icon} #{job_id} {score_pct} — {title[:45]}")
                    except Exception as e:
                        await queue.put(f"✗ #{job_id} error: {e}")
                    finally:
                        completed += 1
                        if completed == total:
                            await queue.put(None)  # sentinel

            tasks = [asyncio.create_task(score_one(jid, t, d)) for jid, t, d in job_data]
            while True:
                msg = await queue.get()
                if msg is None:
                    break
                yield msg
            await asyncio.gather(*tasks)
            yield f"→ Pre-filter skipped {skipped}/{total} jobs (saved ~{skipped} LLM calls)"
        else:
            for job_id, title, description in job_data:
                try:
                    result = fast_score(cv_text, description or "")
                    with get_session() as session:
                        job = session.get(Job, job_id)
                        if job:
                            job.match_score = result.score
                            job.match_explanation = result.explanation
                            link = session.get(JobProfile, (job_id, scoring_profile_id)) if scoring_profile_id else None
                            if link:
                                link.match_score, link.match_explanation = result.score, result.explanation
                                link.screened_at = datetime.utcnow()
                            job.status = JobStatus.SHORTLISTED if result.score >= threshold else JobStatus.ANALYZED
                            if result.score >= threshold:
                                shortlisted += 1
                    score_pct = f"{result.score:.0%}"
                    icon = "⭐" if result.score >= req.min_score else "·"
                    yield f"{icon} #{job_id} {score_pct} — {title[:45]}"
                except Exception as e:
                    yield f"✗ #{job_id} error: {e}"

        yield f"✓ Done — {shortlisted}/{len(job_data)} shortlisted"
    return await sse(gen())


class PurgeRequest(BaseModel):
    max_score: float = 0.1
    dry_run: bool = True


@app.post("/run/purge-archived")
async def run_purge_archived(req: PurgeRequest):
    async def gen():
        from db.models import Job, JobStatus, RawJob, Application, JobEvent
        from db.session import get_session

        # Include NEW/ANALYZED/ARCHIVED — all statuses the user hasn't manually acted on
        _purgeable = [JobStatus.NEW, JobStatus.ANALYZED, JobStatus.ARCHIVED]

        with get_session() as session:
            jobs = (
                session.query(Job)
                .filter(
                    Job.status.in_(_purgeable),
                    Job.match_score.isnot(None),
                    Job.match_score < req.max_score,
                )
                .order_by(Job.match_score.asc())
                .all()
            )
            job_data = [(j.id, j.title, j.match_score, j.status.value) for j in jobs]

        mode = "DRY RUN" if req.dry_run else "DELETE"
        yield f"[{mode}] {len(job_data)} jobs (new/analyzed/archived) with score < {req.max_score:.0%}"

        deleted = 0
        for job_id, title, score, status in job_data:
            if req.dry_run:
                yield f"· #{job_id} {score:.0%} [{status}] — {title[:50]}"
                continue
            try:
                with get_session() as session:
                    session.query(JobEvent).filter(JobEvent.job_id == job_id).delete()
                    session.query(Application).filter(Application.job_id == job_id).delete()
                    session.query(RawJob).filter(RawJob.canonical_id == job_id).delete()
                    job = session.get(Job, job_id)
                    if job:
                        session.delete(job)
                deleted += 1
                yield f"✗ #{job_id} {score:.0%} [{status}] — {title[:50]}"
            except Exception as e:
                yield f"! #{job_id} error: {e}"

        if req.dry_run:
            yield "— preview only, nothing deleted —"
        else:
            yield f"✓ Deleted {deleted}/{len(job_data)} jobs"
    return await sse(gen())


@app.get("/companies/{name}")
async def get_company(name: str):
    from company_service import company_dict, find_company
    from db.session import get_session, init_db
    init_db()
    with get_session() as session:
        row = find_company(session, name)
        if row:
            return company_dict(row)
    return {"name": name, "summary": None}


@app.post("/companies/lookup")
async def lookup_company(body: dict):
    from company_service import enrich_company
    try:
        result, cached = await enrich_company(str(body.get("name", "")), bool(body.get("force")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "cached": cached}


@app.post("/companies/{name}/refresh")
async def refresh_company(name: str):
    from company_service import enrich_company
    result, _ = await enrich_company(name, force=True)
    return {**result, "cached": False}


class CompanyLookupRequest(BaseModel):
    min_score: float = 0.0
    force: bool = False


@app.post("/run/company-lookup")
async def run_company_lookup(req: CompanyLookupRequest = CompanyLookupRequest()):
    async def gen():
        from db.session import get_session, init_db
        from db.models import Job, CompanyInfo
        from company_service import enrich_company, normalize_company_name
        init_db()

        with get_session() as session:
            q = session.query(Job.company).distinct()
            if req.min_score > 0:
                q = q.filter(Job.match_score >= req.min_score)
            all_companies = {row[0] for row in q.all() if row[0]}
            cached = {row[0] for row in session.query(CompanyInfo.normalized_name).all() if row[0]}

        todo = sorted(name for name in all_companies
                      if req.force or normalize_company_name(name) not in cached)
        yield f"Found {len(all_companies)} unique companies, {len(todo)} not yet looked up"

        done = 0
        for name in todo:
            try:
                await enrich_company(name, force=req.force)
                done += 1
                yield f"✓ {name[:50]}"
            except Exception as e:
                yield f"✗ {name[:50]}: {str(e)[:60]}"

        yield f"✓ Done — {done}/{len(todo)} companies looked up"
    return await sse(gen())


class TranslateRequest(BaseModel):
    job_id: int
    target: str = "en"  # "en" or "zh"


@app.post("/run/translate")
async def run_translate(req: TranslateRequest):
    from db.models import Job
    from db.session import get_session
    from llm.router import call_llm
    from llm.prompt_manager import render_prompt

    with get_session() as session:
        job = session.get(Job, req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        description = job.description or ""

    if not description:
        raise HTTPException(400, "No description to translate")

    target_name = "English" if req.target == "en" else "Simplified Chinese (中文)"
    system, user, prompt = render_prompt("translation", target_language=target_name, text=description)
    text, _ = await call_llm(user=user, system=system, max_tokens=prompt["max_tokens"],
                             temperature=prompt["temperature"], operation="translation")
    return {"translated": text}


class CoverRequest(BaseModel):
    job_id: int
    language: str = "en"
    direction: Optional[str] = None


@app.post("/run/cover")
async def run_cover(req: CoverRequest):
    from analyzer.scorer import load_cv_text
    from llm.cover_letter import generate_cover_letter
    from db.models import Job
    from db.session import get_session

    with get_session() as session:
        job = session.get(Job, req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        # Detach
        session.expunge(job)

    cv_text = load_cv_text(direction=req.direction or job.direction or None)
    letter = await generate_cover_letter(job, cv_text, language=req.language,
                                         role=req.direction or job.direction)
    return {"letter": letter}


class TailorCVRequest(BaseModel):
    job_id: int
    direction: Optional[str] = None
    profile_id: Optional[int] = None


@app.post("/run/tailor-cv")
async def run_tailor_cv(req: TailorCVRequest):
    from analyzer.scorer import load_cv_text
    from llm.cv_tailor import tailor_cv
    import json as _json
    from db.models import Job, SearchProfile
    from db.session import get_session

    with get_session() as session:
        job = session.get(Job, req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        profile = None
        if req.profile_id:
            profile = session.get(SearchProfile, req.profile_id)
        if not profile and req.direction:
            profile = session.query(SearchProfile).filter_by(slug=req.direction).first()
        if not profile and job.direction:
            profile = session.query(SearchProfile).filter_by(slug=job.direction).first()
        direction = profile.slug if profile else (req.direction or job.direction or None)
        try:
            sections = _json.loads(profile.cv_sections_json or "{}") if profile else None
        except _json.JSONDecodeError:
            sections = None
        source_profile_id = profile.id if profile else None
        session.expunge(job)

    cv_text = load_cv_text(direction=direction)
    result = await tailor_cv(job, cv_text, cv_sections=sections, role=profile.name if profile else direction)
    result["source_profile_id"] = source_profile_id
    return result


class ApplyEmailRequest(BaseModel):
    job_id: int
    cover_letter: str
    dry_run: bool = True
    recipient_email: Optional[str] = None


@app.post("/run/apply/email")
async def run_apply_email(req: ApplyEmailRequest):
    from applicator.email_apply import send_application
    from db.models import Job
    from db.session import get_session

    with get_session() as session:
        job = session.get(Job, req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        session.expunge(job)

    recipient = req.recipient_email or ""
    subject = f"Application: {job.title} — {settings.apply_from_name or settings.smtp_user}"

    if req.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "recipient": recipient or "(no email — add manually)",
            "subject": subject,
        }

    if not recipient:
        raise HTTPException(400, "recipient_email required for real send")

    ok = await send_application(
        job=job,
        cover_letter=req.cover_letter,
        recipient_email=recipient,
        dry_run=False,
    )
    return {"ok": ok}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=True)


# ── Progress tracking endpoints ────────────────────────────────────────────────

@app.post("/jobs/{job_id}/view")
def mark_viewed(job_id: int):
    """Auto-called when user opens a job. Sets status=viewed and logs the event."""
    from db.session import get_session
    from db.models import Job, JobStatus, JobEvent, ApplicationEvent
    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        # Only upgrade status, never downgrade (don't overwrite applied/interview etc.)
        upgradeable = {JobStatus.NEW, JobStatus.ANALYZED, JobStatus.SHORTLISTED}
        if job.status in upgradeable:
            job.status = JobStatus.VIEWED
            job.viewed_at = datetime.utcnow()
            session.add(JobEvent(
                job_id=job_id,
                event_type=ApplicationEvent.VIEWED,
                note="Opened in UI",
            ))
    return {"ok": True}


@app.post("/jobs/{job_id}/apply")
def mark_applied(job_id: int, body: dict):
    """
    Mark a job as applied. Records method, contact, note on the Application record
    and adds an APPLIED event to the timeline.
    """
    from db.session import get_session
    from db.models import Job, JobStatus, Application, ApplicationStatus, JobEvent, ApplicationEvent
    method = body.get("method", "manual")          # email | form | manual | linkedin
    recipient = body.get("recipient_email", "")
    contact = body.get("contact_name", "")
    cover = body.get("cover_letter", "")
    note = body.get("note", "")

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job.status = JobStatus.APPLIED
        job.applied_at = datetime.utcnow()

        # Upsert application record
        app_rec = session.query(Application).filter(Application.job_id == job_id).first()
        if not app_rec:
            app_rec = Application(job_id=job_id)
            session.add(app_rec)
        app_rec.apply_method = method
        app_rec.recipient_email = recipient
        app_rec.contact_name = contact
        app_rec.cover_letter = cover or app_rec.cover_letter
        app_rec.status = ApplicationStatus.SENT
        app_rec.applied_at = datetime.utcnow()
        app_rec.notes = note

        session.add(JobEvent(
            job_id=job_id,
            event_type=ApplicationEvent.APPLIED,
            note=f"via {method}" + (f" → {recipient}" if recipient else ""),
        ))
    return {"ok": True}


@app.post("/jobs/{job_id}/events")
def add_event(job_id: int, body: dict, response: Response):
    """Add any timeline event (interview, offer, rejection, note...)."""
    from db.session import get_session
    from db.models import Job, JobStatus, JobEvent, ApplicationEvent
    event_type = body.get("event_type")
    note = body.get("note", "")
    occurred_at_str = body.get("occurred_at")  # optional ISO string

    try:
        ev = ApplicationEvent(event_type)
    except ValueError:
        raise HTTPException(400, f"Invalid event_type: {event_type}")

    occurred_at = datetime.utcnow()
    if occurred_at_str:
        try:
            occurred_at = datetime.fromisoformat(occurred_at_str)
        except ValueError:
            pass

    # Auto-update job status for key events
    STATUS_MAP = {
        ApplicationEvent.INTERVIEW_1:     JobStatus.INTERVIEWING,
        ApplicationEvent.INTERVIEW_2:     JobStatus.INTERVIEWING,
        ApplicationEvent.TECHNICAL:       JobStatus.INTERVIEWING,
        ApplicationEvent.OFFER_RECEIVED:  JobStatus.OFFER,
        ApplicationEvent.OFFER_ACCEPTED:  JobStatus.OFFER,
        ApplicationEvent.REJECTED:        JobStatus.REJECTED,
    }

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if ev in STATUS_MAP:
            job.status = STATUS_MAP[ev]
        session.add(JobEvent(
            job_id=job_id,
            event_type=ev,
            note=note,
            occurred_at=occurred_at,
        ))
    response.headers["Access-Control-Allow-Origin"] = "*"
    return {"ok": True}


@app.get("/jobs/{job_id}/events")
def get_events(job_id: int):
    """Return the full timeline for a job."""
    from db.session import get_session
    from db.models import JobEvent
    with get_session() as session:
        events = session.query(JobEvent).filter(
            JobEvent.job_id == job_id
        ).order_by(JobEvent.occurred_at).all()
        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
                "note": e.note,
            }
            for e in events
        ]


@app.get("/tracker")
def get_tracker():
    """
    Return all jobs that have been interacted with (viewed or beyond),
    sorted by last activity, for the tracker board.
    """
    from db.session import get_session
    from db.models import Job, JobStatus, Application
    active_statuses = [
        JobStatus.VIEWED, JobStatus.CONSIDERING, JobStatus.APPLIED,
        JobStatus.INTERVIEWING, JobStatus.OFFER,
        JobStatus.REJECTED,
    ]
    with get_session() as session:
        jobs = (
            session.query(Job)
            .filter(Job.status.in_(active_statuses))
            .order_by(Job.updated_at.desc())
            .all()
        )
        result = []
        for j in jobs:
            app = session.query(Application).filter(Application.job_id == j.id).first()
            result.append({
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "url": j.url,
                "source": j.source,
                "status": j.status,
                "match_score": j.match_score,
                "viewed_at": j.viewed_at.isoformat() if j.viewed_at else None,
                "applied_at": j.applied_at.isoformat() if j.applied_at else None,
                "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                "apply_method": app.apply_method if app else None,
                "recipient_email": app.recipient_email if app else None,
                "contact_name": app.contact_name if app else None,
                "notes": app.notes if app else None,
            })
    return result
