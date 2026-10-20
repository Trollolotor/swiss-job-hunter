"""Additive operational records; histories never replace canonical vacancy IDs."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.models import Base


class JobStatusChange(Base):
    __tablename__ = "job_status_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    previous: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    origin: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class BoardSearchRun(Base):
    __tablename__ = "board_search_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    keyword: Mapped[str] = mapped_column(String(500))
    location: Mapped[str] = mapped_column(String(300))
    profiles_json: Mapped[str] = mapped_column(Text, default="[]")
    outcome: Mapped[str] = mapped_column(String(30), default="running")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class DetailCache(Base):
    __tablename__ = "employer_detail_cache"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("employer_sources.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("connector_versions.id"))
    identity: Mapped[str] = mapped_column(String(2200))
    token: Mapped[str | None] = mapped_column(String(1000))
    posting_json: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("source_id", "version_id", "identity"),)
