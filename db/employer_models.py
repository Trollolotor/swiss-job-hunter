"""Additive source/provenance tables. Import through db.models."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.models import Base


class Employer(Base):
    __tablename__ = "employers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    identity_key: Mapped[str] = mapped_column(String(64), unique=True)
    classification: Mapped[str] = mapped_column(default="unresolved")
    resolution_status: Mapped[str] = mapped_column(default="discovered", index=True)
    official_url: Mapped[str | None] = mapped_column(String(1000))
    confidence: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class EmployerAlias(Base):
    __tablename__ = "employer_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("employers.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized: Mapped[str] = mapped_column(String(300), index=True)
    __table_args__ = (UniqueConstraint("employer_id", "name"),)


class EmployerEvidence(Base):
    __tablename__ = "employer_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("employers.id"), index=True)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(String(2000))
    source: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    historical: Mapped[bool] = mapped_column(default=False)


class EmployerSource(Base):
    __tablename__ = "employer_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("employers.id"), index=True)
    namespace: Mapped[str] = mapped_column(String(500), unique=True)
    portal_url: Mapped[str] = mapped_column(String(2000))
    platform: Mapped[str] = mapped_column(String(100))
    tenant: Mapped[str] = mapped_column(String(300))
    board: Mapped[str] = mapped_column(String(300), default="default")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    policy_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(default=True)
    health: Mapped[str] = mapped_column(default="pending")
    active_version_id: Mapped[int | None] = mapped_column(Integer)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime)
    poll_hours: Mapped[int | None] = mapped_column(Integer)
    next_due_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ConnectorVersion(Base):
    __tablename__ = "connector_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("connector_versions.id"))
    digest: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(default="generated")
    code: Mapped[str] = mapped_column(Text, default="")
    config_json: Mapped[str] = mapped_column(Text)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    sdk_version: Mapped[str] = mapped_column(default="1")
    image_digest: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="candidate")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class SourceRun(Base):
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("connector_versions.id"))
    scope: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(default="running")
    started_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    validation: Mapped[bool] = mapped_column(default=False)


class JobSource(Base):
    __tablename__ = "job_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    employer_id: Mapped[int | None] = mapped_column(ForeignKey("employers.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    namespace: Mapped[str] = mapped_column(String(500))
    posting_id: Mapped[str] = mapped_column(String(2000))
    variant: Mapped[str] = mapped_column(String(200), default="")
    requisition_id: Mapped[str | None] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(2000))
    application_url: Mapped[str | None] = mapped_column(String(2000), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    availability: Mapped[str] = mapped_column(default="unknown")
    missing_checks: Mapped[int] = mapped_column(default=0)
    missing_scope: Mapped[str | None] = mapped_column(String(64))
    content_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    score_stale: Mapped[bool] = mapped_column(default=False)
    historical: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("namespace", "posting_id", "variant"),)


class JobSourceRevision(Base):
    __tablename__ = "job_source_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_source_id: Mapped[int] = mapped_column(ForeignKey("job_sources.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class SourceRunMember(Base):
    __tablename__ = "source_run_members"
    run_id: Mapped[int] = mapped_column(
        ForeignKey("source_runs.id", ondelete="CASCADE"), primary_key=True
    )
    identity: Mapped[str] = mapped_column(String(2200), primary_key=True)


class EmployerTask(Base):
    __tablename__ = "employer_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int | None] = mapped_column(ForeignKey("employers.id"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(default="queued", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    due_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    token: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ConnectorIncident(Base):
    __tablename__ = "connector_incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    category: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(default="open")
    failed_candidates: Mapped[int] = mapped_column(default=0)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class BudgetLedger(Base):
    __tablename__ = "employer_budget_ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("employer_tasks.id"))
    operation: Mapped[str] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(30), default="employer", server_default="employer", index=True)
    context_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    model: Mapped[str] = mapped_column(String(200))
    reserved_usd: Mapped[float] = mapped_column(Float)
    actual_usd: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(default="reserved")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
