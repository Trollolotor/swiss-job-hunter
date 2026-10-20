"""Optional employer operations; mutations queue durable work rather than executing code."""

import os
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect, select, update

from config.runtime import put_setting
from db.models import (
    BudgetLedger,
    ConnectorIncident,
    ConnectorVersion,
    Employer,
    EmployerEvidence,
    EmployerSource,
    EmployerTask,
    JobProfile,
    JobSource,
    SearchProfile,
    SourceRun,
)
from db.session import engine, get_session, init_db
from employers.config import EmployerSettings, get_config
from employers.service import row_dict
from employers.tasks import enqueue

router = APIRouter()


def schema_ready():
    if not inspect(engine).has_table("alembic_version"):
        return False
    from sqlalchemy import text

    with engine.connect() as c:
        return (
            c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "20261021_agent_optimization"
        )


@router.get("/settings/employer-sources")
def settings():
    init_db()
    return {
        **get_config().model_dump(),
        "schema_ready": schema_ready(),
        "runner_configured": bool(os.environ.get("EMPLOYER_RUNNER_IMAGE")),
        "launcher_available": os.path.exists(
            os.environ.get("EMPLOYER_LAUNCHER_SOCKET", "/run/sjh-connector/launcher.sock")
        ),
    }


@router.put("/settings/employer-sources")
def save_settings(body: EmployerSettings):
    init_db()
    if body.enabled and not schema_ready():
        raise HTTPException(409, "Run alembic upgrade head before enabling employer sources")
    previous = get_config()
    put_setting("employer_sources", body.model_dump())
    with get_session() as session:
        if not body.enabled:
            session.execute(update(EmployerTask).where(EmployerTask.status.in_(
                ["queued", "running", "budget_wait"])).values(status="cancelled", token=None))
        if body.coverage != previous.coverage or body.shadow != previous.shadow:
            from db.models import JobSource
            session.execute(update(EmployerTask).where(
                EmployerTask.kind.in_(["collect", "health"]), EmployerTask.status == "running"
            ).values(status="cancelled", token=None))
            session.execute(update(JobSource).where(JobSource.source_id.is_not(None)).values(
                missing_checks=0, missing_scope=None))
        if body.daily_budget_usd:
            session.execute(
                update(EmployerTask)
                .where(EmployerTask.status == "budget_wait")
                .values(status="queued", due_at=datetime.utcnow())
            )
    return settings()


class EmployerBody(BaseModel):
    name: str = Field(min_length=1, max_length=300)


@router.post("/employers")
def add_employer(body: EmployerBody):
    init_db()
    from employers.discovery import observe
    from scrapers.base import ScrapedJob

    with get_session() as session:
        row = observe(
            session,
            ScrapedJob(
                title="Manual employer",
                company=body.name,
                location="",
                description="",
                url="",
                source="manual",
            ),
        )
        if row is None:
            raise HTTPException(422, "An identifiable employer name is required")
        return row_dict(row)


@router.get("/employers")
def list_employers(q: str = "", offset: int = 0, limit: int = 50):
    init_db()
    with get_session() as session:
        query = select(Employer)
        if q:
            query = query.where(Employer.name.ilike(f"%{q}%"))
        rows = session.scalars(
            query.order_by(Employer.last_seen_at.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
        ).all()
        ids = [r.id for r in rows]
        sources = session.scalars(
            select(EmployerSource).where(EmployerSource.employer_id.in_(ids))
        ).all()
        return [
            {**row_dict(r), "sources": [row_dict(s) for s in sources if s.employer_id == r.id]}
            for r in rows
        ]


@router.put("/employer-sources/{source_id}/schedule")
def update_source_schedule(source_id: int, body: dict):
    interval = body.get("poll_hours")
    if interval is not None and (isinstance(interval, bool) or not isinstance(interval, int) or not 2 <= interval <= 4):
        raise HTTPException(422, "poll_hours must be 2, 3, 4, or null for the default")
    with get_session() as session:
        source = session.get(EmployerSource, source_id)
        if not source:
            raise HTTPException(404, "Source not found")
        source.poll_hours = interval
        return {"source_id": source_id, "poll_hours": interval}


@router.get("/employers/operations")
def operations():
    from employers.metrics import source_metrics
    from analyzer.config import get_config as matching_config
    init_db()
    with get_session() as session:
        day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        costs = (
            session.query(
                BudgetLedger.operation,
                func.sum(func.coalesce(BudgetLedger.actual_usd, BudgetLedger.reserved_usd)),
            )
            .filter(BudgetLedger.created_at >= day, BudgetLedger.category == "employer")
            .group_by(BudgetLedger.operation)
            .all()
        )
        outcomes = dict(
            session.query(SourceRun.outcome, func.count(SourceRun.id))
            .filter(SourceRun.validation.is_(False))
            .group_by(SourceRun.outcome)
            .all()
        )
        direct_ids = select(JobSource.job_id).where(
            JobSource.source_id.is_not(None), JobSource.job_id.is_not(None)
        )
        board_ids = select(JobSource.job_id).where(
            JobSource.source_id.is_(None), JobSource.job_id.is_not(None)
        )
        metrics = {
            "daily_cost_upper_bound_usd": sum(float(c[1] or 0) for c in costs),
            "cost_by_operation": {op: float(value or 0) for op, value in costs},
            "collection_outcomes": outcomes,
            "direct_vacancies": session.query(func.count(func.distinct(JobSource.job_id)))
            .filter(JobSource.source_id.is_not(None))
            .scalar()
            or 0,
            "direct_only_vacancies": session.query(func.count(func.distinct(JobSource.job_id)))
            .filter(JobSource.source_id.is_not(None), ~JobSource.job_id.in_(board_ids))
            .scalar()
            or 0,
            "relevant_direct_vacancies": session.query(func.count(func.distinct(JobProfile.job_id)))
            .filter(
                JobProfile.job_id.in_(direct_ids),
                JobProfile.profile_id.in_(select(SearchProfile.id).where(SearchProfile.active.is_(True))),
                JobProfile.match_score >= matching_config().shortlist_threshold,
                JobProfile.score_method == "llm",
                JobProfile.score_stale.is_(False),
            )
            .scalar()
            or 0,
        }
        return {
            "value": source_metrics(session),
            "metrics": metrics,
            "tasks": [
                row_dict(t)
                for t in session.scalars(
                    select(EmployerTask).order_by(EmployerTask.id.desc()).limit(30)
                )
            ],
            "incidents": [
                row_dict(t)
                for t in session.scalars(
                    select(ConnectorIncident).order_by(ConnectorIncident.id.desc()).limit(30)
                )
            ],
            "budget": [
                row_dict(t)
                for t in session.scalars(
                    select(BudgetLedger).where(BudgetLedger.category == "employer").order_by(BudgetLedger.id.desc()).limit(30)
                )
            ],
        }


@router.get("/employers/{employer_id}")
def employer_detail(employer_id: int):
    with get_session() as session:
        row = session.get(Employer, employer_id)
        if not row:
            raise HTTPException(404, "Employer not found")
        return {
            **row_dict(row),
            "evidence": [
                row_dict(v)
                for v in session.scalars(
                    select(EmployerEvidence)
                    .where(EmployerEvidence.employer_id == employer_id)
                    .limit(100)
                )
            ],
            "sources": [
                row_dict(v)
                for v in session.scalars(
                    select(EmployerSource).where(EmployerSource.employer_id == employer_id)
                )
            ],
        }


class ConnectBody(BaseModel):
    official_url: str = Field(min_length=8, max_length=2000)
    portal_url: str = Field(min_length=8, max_length=2000)


@router.post("/employers/{employer_id}/connect")
async def connect(employer_id: int, body: ConnectBody):
    from employers.service import verify_and_create

    try:
        source_id = await verify_and_create(employer_id, body.official_url, body.portal_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"source_id": source_id, "task_id": enqueue("onboard", source_id=source_id)}


@router.post("/employers/{employer_id}/resolve")
def resolve(employer_id: int):
    with get_session() as session:
        if not session.get(Employer, employer_id):
            raise HTTPException(404, "Employer not found")
    return {"task_id": enqueue("resolve", employer_id=employer_id)}


@router.post("/employer-sources/backfill")
def backfill():
    return {"task_id": enqueue("discover")}


@router.get("/employer-sources/{source_id}")
def source_detail(source_id: int):
    with get_session() as session:
        source = session.get(EmployerSource, source_id)
        if not source:
            raise HTTPException(404, "Source not found")
        return {
            **row_dict(source),
            "runs": [
                row_dict(v)
                for v in session.scalars(
                    select(SourceRun)
                    .where(SourceRun.source_id == source_id)
                    .order_by(SourceRun.id.desc())
                    .limit(30)
                )
            ],
            "versions": [
                row_dict(v)
                for v in session.scalars(
                    select(ConnectorVersion)
                    .where(ConnectorVersion.source_id == source_id)
                    .order_by(ConnectorVersion.id.desc())
                    .limit(10)
                )
            ],
        }


@router.post("/employer-sources/{source_id}/versions/{version_id}/validate")
def validate_version(source_id: int, version_id: int):
    with get_session() as session:
        source = session.get(EmployerSource, source_id)
        version = session.get(ConnectorVersion, version_id)
        if not source or not version or version.source_id != source_id:
            raise HTTPException(404, "Source connector version not found")
        if not source.enabled:
            raise HTTPException(409, "Resume the source before validating")
        session.execute(update(EmployerTask).where(
            EmployerTask.source_id == source_id,
            EmployerTask.kind.in_(["repair", "onboard"]),
            EmployerTask.status == "queued"
        ).values(status="cancelled", token=None,
                 error="Superseded by explicit validation of an existing immutable version"))
    return {"task_id":enqueue("validate", source_id=source_id, payload={"version_id":version_id})}


@router.post("/employer-sources/{source_id}/{action}")
def source_action(
    source_id: int, action: Literal["pause", "resume", "retry", "collect", "rollback"]
):
    payload = {}
    with get_session() as session:
        source = session.get(EmployerSource, source_id)
        if not source:
            raise HTTPException(404, "Source not found")
        if action == "pause":
            source.enabled = False
            session.execute(
                update(EmployerTask)
                .where(
                    EmployerTask.source_id == source_id,
                    EmployerTask.status.in_(["queued", "running", "budget_wait"]),
                )
                .values(status="cancelled", token=None)
            )
            return {"paused": True}
        if action in {"resume", "retry"}:
            source.enabled = True
            source.health = "pending"
            if action == "retry":
                for incident in session.scalars(
                    select(ConnectorIncident).where(
                        ConnectorIncident.source_id == source_id,
                        ConnectorIncident.status.in_(["open", "needs_review"]),
                    )
                ):
                    # Only explicit Retry starts a new audit; resume preserves limits.
                    incident.status = "manually_retried"
        elif not source.enabled:
            raise HTTPException(409, "Resume the source before collecting")
        if action == "rollback":
            current = session.get(ConnectorVersion, source.active_version_id)
            if not current or not current.parent_id:
                raise HTTPException(409, "No previous connector is available")
            payload = {"version_id": current.parent_id}
        kind = (
            "rollback"
            if action == "rollback"
            else (
                "revalidate"
                if action == "retry" and source.active_version_id
                else ("collect" if source.active_version_id else "onboard")
            )
        )
    return {"task_id": enqueue(kind, source_id=source_id, payload=payload)}
