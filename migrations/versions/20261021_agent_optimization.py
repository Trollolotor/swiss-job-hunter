"""Profile evaluation, extraction quality, status history, and collection caching."""

from alembic import op
from sqlalchemy import inspect

revision = "20261021_agent_optimization"
down_revision = "20261020_employer_sources"
branch_labels = None
depends_on = None

ADDITIONS = {
    "jobs": {
        "description_quality": "VARCHAR(30) NOT NULL DEFAULT 'unknown'",
        "extraction_json": "TEXT NOT NULL DEFAULT '{}'",
        "enrichment_attempts": "INTEGER NOT NULL DEFAULT 0",
        "enrichment_due_at": "DATETIME",
        "status_origin": "VARCHAR(30) NOT NULL DEFAULT 'legacy_unknown'",
    },
    "job_profiles": {
        "keyword_score": "FLOAT",
        "score_method": "VARCHAR(30) NOT NULL DEFAULT 'legacy'",
        "evaluation_state": "VARCHAR(30) NOT NULL DEFAULT 'legacy'",
        "cv_hash": "VARCHAR(64)",
        "content_hash": "VARCHAR(64)",
        "prompt_revision": "INTEGER",
        "requirements_json": "TEXT NOT NULL DEFAULT '[]'",
    },
    "employer_sources": {"baseline_at": "DATETIME", "poll_hours": "INTEGER"},
    "employer_budget_ledger": {
        "category": "VARCHAR(30) NOT NULL DEFAULT 'employer'",
        "context_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}


def add_columns(bind):
    from sqlalchemy import text

    inspector = inspect(bind)
    for table, additions in ADDITIONS.items():
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        for name, sql_type in additions.items():
            if name not in columns:
                bind.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def upgrade():
    from db.models import Base

    add_columns(op.get_bind())
    Base.metadata.create_all(
        op.get_bind(),
        tables=[
            Base.metadata.tables[n]
            for n in ("job_status_changes", "board_search_runs", "employer_detail_cache")
        ],
        checkfirst=True,
    )


def downgrade():
    raise RuntimeError(
        "Keep additive history on application rollback; restore a verified backup if needed"
    )
