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
from pydantic import BaseModel, Field
from sqlalchemy import func

from config.settings import settings
from job_lifecycle import set_scored_status


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
from employers.api import router as employers_router
app.include_router(employers_router)


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
    direct_only: bool = False, review_queue: bool = False, min_score: float = 0.0,
    source: str = "all",
):
    from employers.ingestion import job_source_fields
    from analyzer.freshness import freshness, priority, utc_naive
    from config.runtime import get_priority_config
    from db.session import get_session
    from db.models import Job, JobStatus, SearchProfile, CompanyInfo, JobProfile, JobSource
    from company_service import company_dict, normalize_company_name
    from sqlalchemy import or_
    from sqlalchemy.orm import selectinload

    config = get_priority_config()
    from analyzer.matching import aggregate
    with get_session() as session:
        query = session.query(Job).options(selectinload(Job.profiles).load_only(
            SearchProfile.id, SearchProfile.slug, SearchProfile.name))
        if status == "active":
            query = query.filter(Job.status != JobStatus.ARCHIVED)
        elif status != "all":
            query = query.filter(Job.status == status)
        if source != "all":
            membership = session.query(JobSource.id).filter(JobSource.job_id == Job.id)
            if source == "employer":
                query = query.filter(membership.filter(JobSource.source_id.isnot(None)).exists())
            else:
                # A canonical job may have sightings on several boards and an
                # employer site. Filter membership without changing its content
                # authority, preferred application link, or primary source.
                query = query.filter(or_(
                    Job.source == source,
                    membership.filter(JobSource.namespace.in_([source, f"board:{source}"])).exists(),
                ))
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
        for row in session.query(JobProfile).join(SearchProfile, SearchProfile.id == JobProfile.profile_id).filter(
            JobProfile.job_id.in_([j.id for j in jobs]),
            (SearchProfile.active.is_(True) if not profile_id and direction in ("all", "", None) else True)):
            profile_matches.setdefault(row.job_id, []).append(row)
        selected_profile_id = profile_id
        if not selected_profile_id and direction not in ("all", "", None):
            selected_profile = session.query(SearchProfile).filter_by(slug=direction).first()
            selected_profile_id = selected_profile.id if selected_profile else None
        company_rows = session.query(CompanyInfo).all()
        companies = {row.normalized_name or normalize_company_name(row.name): company_dict(row)
                     for row in company_rows}
        source_rows = {}
        for row in session.query(JobSource).filter(JobSource.job_id.in_([j.id for j in jobs])).all():
            source_rows.setdefault(row.job_id, []).append(row)
        records = []
        for j in jobs:
            fresh_score, age_label, age_days = freshness(j.posted_at, config)
            if published_within is not None:
                if age_days is None and not include_unknown_dates:
                    continue
                if age_days is not None and age_days > published_within:
                    continue
            evaluation = aggregate(j, profile_matches.get(j.id, []), selected_profile_id)
            match_score = evaluation["match_score"]
            provenance = job_source_fields(session, j.id, source_rows.get(j.id, []))
            if direct_only and not provenance["direct_only"]:
                continue
            if review_queue and (j.expired_at or j.status not in {
                JobStatus.NEW, JobStatus.ANALYZED, JobStatus.SHORTLISTED, JobStatus.VIEWED}):
                continue
            if min_score and (match_score is None or match_score < min_score):
                continue
            records.append({
                **provenance,
                **evaluation,
                "description_quality": j.description_quality,
                "extraction": json.loads(j.extraction_json or "{}"),
                "status_origin": j.status_origin,
                "matching_profile": next(({"id": p.id, "name": p.name} for p in j.profiles
                    if p.id == evaluation["matching_profile_id"]), None),
                "id": j.id, "title": j.title, "company": j.company,
                "location": j.location, "description": j.description, "url": j.url,
                "source": j.source, "source_job_id": j.source_job_id,
                "salary_raw": j.salary_raw, "employment_type": j.employment_type,
                "status": j.status, "match_score": match_score,
                "match_explanation": evaluation["match_explanation"], "user_stars": j.user_stars,
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
                "priority_score": priority(match_score if match_score is not None else evaluation["keyword_score"], fresh_score, config),
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
    direct_only: bool = False, review_queue: bool = False, min_score: float = 0.0,
    source: str = "all",
):
    from db.session import init_db, get_session
    from analyzer.matching import resolve_profiles, ProfileSelectionError
    init_db()
    if profile_id is not None or direction not in ("all", "", None):
        with get_session() as session:
            try:
                resolve_profiles(session, profile_id, direction, require_cv=False)
            except ProfileSelectionError as exc:
                raise HTTPException(422, str(exc)) from exc
    try:
        days = int(published_within) if published_within.strip() else None
    except ValueError as exc:
        raise HTTPException(422, "published_within must be a number of days") from exc
    return get_jobs_query(status, q, direction, min_stars, profile_id, days, sort,
                          include_unknown_dates, direct_only, review_queue, min_score, source)


@app.get("/stats")
def get_stats(threshold: float = 0.1, profile_id: Optional[int] = None, direction: str = "all",
              status: str = "all", q: str = "", direct_only: bool = False,
              review_queue: bool = False, min_stars: int = 0, published_within: str = "",
              include_unknown_dates: bool = True, min_score: float = 0, source: str = "all"):
    from collections import Counter
    records = list_jobs(status=status, q=q, profile_id=profile_id, direction=direction,
                        direct_only=direct_only, review_queue=review_queue, min_stars=min_stars,
                        published_within=published_within, include_unknown_dates=include_unknown_dates,
                        min_score=min_score, source=source)
    scores = [r["match_score"] for r in records if r["match_score"] is not None]
    return {"total": len(records), "by_status": dict(Counter(r["status"] for r in records)),
            "by_source": dict(Counter(r["source"] for r in records)),
            "avg_score": sum(scores) / len(scores) if scores else None,
            "top_score": max(scores) if scores else None,
            "above_threshold": sum(v >= threshold for v in scores), "threshold": threshold}


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
        from job_lifecycle import transition
        transition(job, s, origin="user", reason=body.get("reason") or "Manual review")
        if s == JobStatus.APPLIED and job.applied_at is None:
            # Dragging to Applied records a decision, without replacing a
            # detailed application or changing when an existing one was sent.
            from db.models import Application
            application = session.query(Application).filter_by(job_id=job_id).first()
            job.applied_at = (
                application.applied_at if application and application.applied_at else datetime.utcnow()
            )
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
    from analyzer.matching import resolve_profiles, ProfileSelectionError
    from db.session import get_session, init_db
    init_db()
    with get_session() as session:
        try:
            selected = resolve_profiles(session, req.profile_id, req.direction, require_cv=False, allow_empty=True)
            profile_ids = [p.id for p in selected]
            active_direction = selected[0].slug if len(selected) == 1 else None
        except ProfileSelectionError as exc:
            raise HTTPException(422, str(exc)) from exc

    async def gen():
        from scrapers import SCRAPER_REGISTRY
        from scrapers.collection import collect_board
        total_new = 0
        for keyword in req.keywords or [req.keyword]:
            for source_name in req.sources:
                cls = SCRAPER_REGISTRY.get(source_name)
                if not cls:
                    yield f"✗ Unknown source: {source_name}"
                    continue
                kwargs = {"time_range": req.linkedin_time_range,
                          "experience_level": req.linkedin_experience_level} if source_name == "linkedin.com" else {}
                yield f"→ {source_name}: {keyword} · {req.location}"
                async with cls(**kwargs) as scraper:
                    try:
                        async for scraped, job, created in collect_board(scraper, keyword, req.location,
                            req.pages, direction=active_direction, profile_ids=profile_ids,
                            max_age_days=req.max_age_days, include_unknown_dates=req.include_unknown_dates):
                            if created:
                                total_new += 1
                                yield f"  + #{job.id} {scraped.title[:70]} @ {scraped.company}"
                    except Exception as exc:
                        yield f"✗ {source_name}: {str(exc)[:200]}"
                    yield "RUN " + json.dumps({"source": source_name, **scraper.report})
        yield f"✓ Found {total_new} new jobs"
    return await sse(gen())


class EnrichRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=20000)
    source: str = "jobs.ch"
    rescore_llm: bool = False
    direction: Optional[str] = None
    profile_id: int | None = None
    check_availability: bool = False


@app.post("/run/enrich")
async def run_enrich(req: EnrichRequest):
    from analyzer.matching import resolve_profiles, ProfileSelectionError, evaluate
    from analyzer.enrichment import enrich
    from analyzer.quality import needs_enrichment
    from db.models import Job
    from db.session import get_session
    with get_session() as session:
        try:
            pids = [p.id for p in resolve_profiles(session, req.profile_id, req.direction)] if req.rescore_llm else []
        except ProfileSelectionError as exc:
            raise HTTPException(422, str(exc)) from exc
        jobs = session.query(Job).filter_by(source=req.source).order_by(Job.scraped_at.desc()).all()
        ids = [j.id for j in jobs if req.check_availability or needs_enrichment(j)][:req.limit]
    async def gen():
        yield f"Enriching {len(ids)} vacancies"
        for jid in ids:
            try:
                complete = await enrich(jid, availability=req.check_availability)
                yield f"#{jid}: {'complete description' if complete else 'incomplete/closed; recovery state retained'}"
                if complete:
                    for pid in pids:
                        result = await evaluate(jid, pid, llm=True)
                        yield json.dumps({"job_id": jid, "profile_id": pid, **result})
            except Exception as exc:
                yield f"✗ #{jid}: {str(exc)[:200]}"
    return await sse(gen())


class AnalyzeRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=20000)
    llm: bool = False
    min_score: float | None = Field(default=None, ge=0, le=1)
    skip_scored: bool = True
    archive_below: float | None = Field(default=None, ge=0, le=1)
    min_keyword_score: float = 0.0  # compatibility only: keyword estimates never reject
    direction: Optional[str] = None
    profile_id: int | None = None
    concurrency: int | None = Field(default=None, ge=1, le=20)


@app.post("/run/analyze")
async def run_analyze(req: AnalyzeRequest):
    from analyzer.matching import resolve_profiles, ProfileSelectionError, evaluate
    from analyzer.config import get_config
    from db.models import Job, JobProfile
    from db.session import get_session, init_db
    init_db()
    config = get_config()
    if req.min_score is not None and req.archive_below is not None and req.archive_below > req.min_score:
        raise HTTPException(422, "archive threshold must not exceed shortlist threshold")
    with get_session() as session:
        try:
            profile_ids = [p.id for p in resolve_profiles(session, req.profile_id, req.direction)]
        except ProfileSelectionError as exc:
            raise HTTPException(422, str(exc)) from exc
        jobs = session.query(Job).order_by(Job.scraped_at.desc()).limit(req.limit).all()
        payloads = []
        for job in jobs:
            for pid in profile_ids:
                link = session.get(JobProfile, (job.id, pid))
                if req.skip_scored and link and link.score_method == "llm" and not link.score_stale:
                    continue
                payloads.append((job.id, pid))

    async def gen():
        sem = asyncio.Semaphore(req.concurrency or config.concurrency)
        queue = asyncio.Queue()
        async def one(jid, pid):
            async with sem:
                try:
                    result = await evaluate(jid, pid, llm=req.llm, force=not req.skip_scored,
                                            shortlist=req.min_score, archive=req.archive_below)
                    await queue.put(json.dumps({"job_id": jid, "profile_id": pid, **result}))
                except Exception as exc:
                    await queue.put(f"✗ #{jid} profile {pid}: {str(exc)[:200]}")
        yield f"Evaluating {len(payloads)} vacancy/profile pairs"
        workers = [asyncio.create_task(one(*p)) for p in payloads]
        try:
            for _ in workers:
                yield await queue.get()
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        yield "✓ Evaluation finished; pending extraction/budget work remains eligible"
    return await sse(gen())


class PurgeRequest(BaseModel):
    max_score: float = 0.1
    dry_run: bool = True


@app.post("/run/purge-archived")
async def run_purge_archived(req: PurgeRequest):
    async def gen():
        from db.models import Job, JobProfile, JobStatus, RawJob, Application, JobEvent
        from db.session import get_session

        # Include NEW/ANALYZED/ARCHIVED — all statuses the user hasn't manually acted on
        from sqlalchemy import or_
        _purgeable = [JobStatus.NEW, JobStatus.ANALYZED, JobStatus.ARCHIVED]

        with get_session() as session:
            jobs = (
                session.query(Job)
                .filter(
                    Job.status.in_(_purgeable),
                    Job.status_origin.notin_(["user", "legacy_unknown"]),
                    Job.description_quality == "complete",
                    ~Job.id.in_(session.query(JobProfile.job_id).filter(or_(
                        JobProfile.score_stale.is_(True), JobProfile.score_method != "llm"))),
                    Job.match_score.isnot(None),
                    Job.match_score < req.max_score,
                    ~Job.application.has(),
                    ~Job.events.any(),
                    Job.user_stars.is_(None),
                    Job.viewed_at.is_(None),
                    Job.applied_at.is_(None),
                )
                .order_by(Job.match_score.asc())
                .all()
            )
            from analyzer.matching import all_profiles_below
            job_data = [(j.id, j.title, j.match_score, j.status.value) for j in jobs
                        if all_profiles_below(session, j, req.max_score)]

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
            from job_lifecycle import transition
            transition(job, JobStatus.VIEWED, origin="user", reason="Vacancy opened")
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
        from job_lifecycle import transition
        transition(job, JobStatus.APPLIED, origin="user", reason="Application recorded")
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
            from job_lifecycle import transition
            transition(job, STATUS_MAP[ev], origin="user", reason="Application event recorded")
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
    Return decision stages and archived jobs with prior user interaction.
    """
    from db.session import get_session
    from db.models import Job, JobStatus, Application, JobSource
    from employers.ingestion import job_source_fields
    from sqlalchemy import and_, or_
    active_statuses = [
        JobStatus.CONSIDERING, JobStatus.APPLIED,
        JobStatus.INTERVIEWING, JobStatus.OFFER,
        JobStatus.REJECTED,
    ]
    with get_session() as session:
        jobs = (
            session.query(Job)
            .filter(or_(
                Job.status.in_(active_statuses),
                and_(Job.status == JobStatus.ARCHIVED, or_(
                    Job.status_origin == "user", Job.viewed_at.isnot(None),
                    Job.applied_at.isnot(None), Job.user_stars > 0,
                    Job.application.has(), Job.events.any(),
                )),
            ))
            .order_by(Job.updated_at.desc())
            .all()
        )
        source_rows = {}
        for row in session.query(JobSource).filter(JobSource.job_id.in_([j.id for j in jobs])).all():
            source_rows.setdefault(row.job_id, []).append(row)
        result = []
        for j in jobs:
            app = session.query(Application).filter(Application.job_id == j.id).first()
            result.append({
                **job_source_fields(session, j.id, source_rows.get(j.id, [])),
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
