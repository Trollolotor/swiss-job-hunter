"""
Database session factory and helpers.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
from typing import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from db.models import Base


def _get_engine() -> Engine:
    db_url = settings.database_url
    # Ensure data directory exists for SQLite
    if db_url.startswith("sqlite"):
        db_path = db_url.replace("sqlite:///", "")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if "sqlite" in db_url else {},
        echo=False,
    )

    # Enable WAL mode for better SQLite concurrency
    if "sqlite" in db_url:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn, _):  # type: ignore[misc]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """Create/migrate tables and import legacy direction CVs. Idempotent."""
    Base.metadata.create_all(bind=engine)
    columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    if "posted_at_source" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE jobs ADD COLUMN posted_at_source VARCHAR(30) DEFAULT 'unknown'"
            ))
    profile_columns = {c["name"] for c in inspect(engine).get_columns("search_profiles")}
    profile_additions = {
        "cv_sections_json": "TEXT DEFAULT '{}'",
        "default_location": "VARCHAR(200) DEFAULT 'Zürich'",
        "parent_profile_id": "INTEGER",
        "tailored_for_job_id": "INTEGER",
    }
    with engine.begin() as conn:
        for name, sql_type in profile_additions.items():
            if name not in profile_columns:
                conn.execute(text(f"ALTER TABLE search_profiles ADD COLUMN {name} {sql_type}"))
    _import_legacy_profiles()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-") or "default"


def _import_legacy_profiles() -> None:
    """Seed profiles from cv_*.txt/cv.txt without overwriting UI edits."""
    from config.settings import settings
    from cv_sections import compile_cv_text, normalize_keywords, parse_legacy_cv
    from db.models import Job, JobProfile, SearchProfile

    candidates: dict[str, Path] = {}
    data_dir = settings.cv_text_path.parent
    for path in data_dir.glob("cv_*.txt"):
        candidates[_slug(path.stem[3:])] = path
    if settings.cv_text_path.exists():
        candidates.setdefault("default", settings.cv_text_path)

    with SessionLocal.begin() as session:
        for slug, path in candidates.items():
            if session.query(SearchProfile).filter_by(slug=slug).first():
                continue
            keywords = settings.keyword_presets.get(slug, [])
            cv_text = path.read_text(encoding="utf-8")
            sections = parse_legacy_cv(cv_text)
            session.add(SearchProfile(
                slug=slug,
                name=slug.replace("-", " ").title(),
                keywords_json=json.dumps(normalize_keywords(keywords), ensure_ascii=False),
                cv_text=compile_cv_text(sections),
                cv_sections_json=json.dumps(sections, ensure_ascii=False),
                default_location=settings.default_location,
            ))
        session.flush()
        for profile in session.query(SearchProfile).all():
            try:
                keywords = normalize_keywords(json.loads(profile.keywords_json or "[]"))
            except (TypeError, json.JSONDecodeError):
                keywords = []
            profile.keywords_json = json.dumps(keywords, ensure_ascii=False)
            try:
                sections = json.loads(profile.cv_sections_json or "{}")
            except (TypeError, json.JSONDecodeError):
                sections = {}
            if not any(str(v).strip() for v in sections.values()):
                sections = parse_legacy_cv(profile.cv_text or "")
                profile.cv_sections_json = json.dumps(sections, ensure_ascii=False)
                profile.cv_text = compile_cv_text(sections)
            if profile.default_location is None:
                profile.default_location = settings.default_location
        profiles = {p.slug: p.id for p in session.query(SearchProfile).all()}
        for job_id, direction in session.query(Job.id, Job.direction).filter(Job.direction.isnot(None)):
            profile_id = profiles.get(_slug(direction or ""))
            if profile_id and not session.get(JobProfile, (job_id, profile_id)):
                session.add(JobProfile(job_id=job_id, profile_id=profile_id))




@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
