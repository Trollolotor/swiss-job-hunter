"""One profile-aware evaluation pipeline for API, scheduler, CLI, and ingestion."""

import json
from datetime import datetime

from db.models import Job, JobProfile, JobStatus, SearchProfile
from employers.contracts import digest


class ProfileSelectionError(ValueError):
    pass


def resolve_profiles(
    session, profile_id=None, direction=None, *, require_cv=True, allow_empty=False
):
    direction = None if direction in (None, "", "all") else direction
    by_id = session.get(SearchProfile, profile_id) if profile_id is not None else None
    by_slug = session.query(SearchProfile).filter_by(slug=direction).first() if direction else None
    if (profile_id is not None and not by_id) or (direction and not by_slug):
        raise ProfileSelectionError("Search profile not found")
    if by_id and by_slug and by_id.id != by_slug.id:
        raise ProfileSelectionError("profile_id and direction refer to different profiles")
    profiles = (
        [by_id or by_slug]
        if by_id or by_slug
        else session.query(SearchProfile).filter_by(active=True).order_by(SearchProfile.id).all()
    )
    if not profiles and not allow_empty:
        raise ProfileSelectionError("Create an active CV profile before matching")
    if require_cv and any(not p.cv_text.strip() for p in profiles):
        raise ProfileSelectionError("The selected profile has no CV text")
    return profiles


def input_hash(job):
    return digest([job.title, job.description, job.location, job.employment_type, job.salary_raw])


def local_evaluate(session, job, profile):
    from analyzer.scorer import fast_score

    link = session.get(JobProfile, (job.id, profile.id))
    if not link:
        link = JobProfile(job_id=job.id, profile_id=profile.id)
        session.add(link)
        session.flush()
    cv_hash, content = digest(profile.cv_text), input_hash(job)
    if (link.cv_hash, link.content_hash) != (cv_hash, content):
        link.score_stale = link.score_method in {"llm", "legacy"}
    result = fast_score(profile.cv_text, job.description or "")
    link.keyword_score = result.score if job.description_quality == "complete" else None
    if link.score_method not in {"llm", "legacy"} or link.score_stale:
        link.evaluation_state = "pending" if job.description_quality == "complete" else "deferred"
        # Retain stale scores as historical evidence, but never display them as current fit.
        if link.score_method not in {"llm", "legacy"}:
            link.match_score, link.match_explanation = None, result.explanation
            link.score_method = "keyword"
            link.cv_hash, link.content_hash = cv_hash, content
    return link


def aggregate(job, links, profile_id=None):
    selected = [r for r in links if r.profile_id == profile_id] if profile_id else links
    current = [r for r in selected if not r.score_stale and r.match_score is not None]
    confirmed = [r for r in current if r.score_method == "llm"]
    candidates = confirmed or [r for r in current if r.score_method == "legacy"]
    best = max(candidates, key=lambda r: r.match_score) if candidates else None
    provisional_link = max(
        (r for r in selected if r.keyword_score is not None),
        key=lambda r: r.keyword_score,
        default=None,
    )
    provisional = provisional_link.keyword_score if provisional_link else None
    pending_link = provisional_link or next(iter(selected), None)
    return {
        "match_score": best.match_score if best else None,
        "match_explanation": best.match_explanation if best else None,
        "keyword_score": provisional,
        "score_method": best.score_method
        if best
        else "keyword"
        if provisional is not None
        else "pending",
        "evaluation_state": "scored"
        if best and best.score_method == "llm"
        else "legacy"
        if best
        else (pending_link.evaluation_state if pending_link else "pending")
        if job.description_quality == "complete"
        else "deferred",
        "matching_profile_id": best.profile_id
        if best
        else provisional_link.profile_id
        if provisional_link
        else None,
        "requirements": json.loads(best.requirements_json or "[]") if best else [],
        "score_stale": best.score_stale if best else any(r.score_stale for r in selected),
    }


def confirmed_links(job, profiles, links):
    return [
        r
        for p in profiles
        if (r := links.get(p.id))
        and r.score_method == "llm"
        and not r.score_stale
        and r.evaluation_state == "scored"
        and r.match_score is not None
        and r.cv_hash == digest(p.cv_text)
        and r.content_hash == input_hash(job)
    ]


def all_profiles_below(session, job, threshold):
    profiles = session.query(SearchProfile).filter_by(active=True).all()
    links = {row.profile_id: row for row in session.query(JobProfile).filter_by(job_id=job.id)}
    confirmed = confirmed_links(job, profiles, links)
    return (
        bool(profiles)
        and len(confirmed) == len(profiles)
        and all(row.match_score < threshold for row in confirmed)
    )


def update_workflow(session, job, shortlist=None, archive=None):
    from analyzer.config import get_config
    from job_lifecycle import transition

    config = get_config()
    shortlist = config.shortlist_threshold if shortlist is None else shortlist
    archive = config.archive_threshold if archive is None else archive
    profiles = session.query(SearchProfile).filter_by(active=True).all()
    links = {r.profile_id: r for r in session.query(JobProfile).filter_by(job_id=job.id)}
    confirmed = confirmed_links(job, profiles, links)
    if any(r.match_score >= shortlist for r in confirmed):
        transition(
            job,
            JobStatus.SHORTLISTED,
            origin="scoring",
            reason="At least one active CV meets the shortlist threshold",
        )
    elif (
        profiles
        and len(confirmed) == len(profiles)
        and all(r.match_score < archive for r in confirmed)
    ):
        transition(
            job,
            JobStatus.ARCHIVED,
            origin="scoring",
            reason="All active CV evaluations are below the archive threshold",
        )
    elif confirmed:
        transition(job, JobStatus.ANALYZED, origin="scoring", reason="Updated profile evaluation")
    result = aggregate(job, [links[p.id] for p in profiles if p.id in links])
    job.match_score, job.match_explanation = result["match_score"], result["match_explanation"]


async def evaluate(job_id, profile_id, *, llm=False, force=False, shortlist=None, archive=None):
    from analyzer.scorer import llm_score
    from db.session import get_session

    with get_session() as session:
        job = session.get(Job, job_id)
        if not job:
            return {"state": "missing"}
        profile = resolve_profiles(session, profile_id)[0]
        link = local_evaluate(session, job, profile)
        if not llm or job.description_quality != "complete":
            return {"state": link.evaluation_state, "keyword_score": link.keyword_score}
        if not force and link.score_method == "llm" and not link.score_stale:
            return {"state": "cached", "score": link.match_score}
        cv, title, desc, role = profile.cv_text, job.title, job.description, profile.name
        before = digest(cv), input_hash(job)
    from llm.spending import context

    token = context.set(
        {
            "job_id": job_id,
            "profile_id": profile_id,
            "cv_hash": before[0],
            "content_hash": before[1],
        }
    )
    try:
        result = await llm_score(cv, title, desc, role=role)
    except Exception as exc:
        from llm.spending import SpendingUnavailable

        with get_session() as session:
            link = session.get(JobProfile, (job_id, profile_id))
            if link:
                link.evaluation_state = (
                    "budget_wait" if isinstance(exc, SpendingUnavailable) else "error"
                )
        raise
    finally:
        context.reset(token)
    with get_session() as session:
        job, profile = session.get(Job, job_id), session.get(SearchProfile, profile_id)
        if not job or not profile or before != (digest(profile.cv_text), input_hash(job)):
            return {"state": "stale_result"}
        link = session.get(JobProfile, (job_id, profile_id))
        link.match_score, link.match_explanation = result.score, result.explanation
        link.score_method, link.evaluation_state, link.score_stale = "llm", "scored", False
        link.cv_hash, link.content_hash, link.screened_at = *before, datetime.utcnow()
        link.requirements_json = json.dumps(result.requirements or [], ensure_ascii=False)
        from llm.prompt_manager import get_prompt

        link.prompt_revision = get_prompt("job_screening")["revision"]
        from employers.ingestion import mark_scores_fresh

        mark_scores_fresh(session, job_id, profile_id)
        session.flush()
        update_workflow(session, job, shortlist, archive)
    return {"state": "scored", "score": result.score}
