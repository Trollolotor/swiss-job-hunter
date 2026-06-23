"""Prompts, automation, company intelligence and vacancy lifecycle."""
from alembic import op
from sqlalchemy import inspect

revision = "20260622_intelligence"
down_revision = "20260622_profile_cv"
branch_labels = None
depends_on = None


def _add_columns(table: str, additions: dict[str, str]) -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns(table)}
    for name, sql_type in additions.items():
        if name not in columns:
            op.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def upgrade() -> None:
    from db.models import Base
    Base.metadata.create_all(bind=op.get_bind())
    _add_columns("jobs", {
        "posted_at_raw": "VARCHAR(300)", "posted_at_confidence": "FLOAT DEFAULT 0.0",
        "last_seen_at": "DATETIME", "availability_checked_at": "DATETIME",
        "unavailable_checks": "INTEGER DEFAULT 0", "expired_at": "DATETIME",
        "expired_reason": "VARCHAR(300)",
    })
    _add_columns("company_info", {
        "normalized_name": "VARCHAR(300)", "aliases_json": "TEXT DEFAULT '[]'",
        "industry": "VARCHAR(200)", "scope": "VARCHAR(30)", "model": "VARCHAR(200)",
        "prompt_revision": "INTEGER",
    })
    _add_columns("job_profiles", {
        "match_score": "FLOAT", "match_explanation": "TEXT", "screened_at": "DATETIME",
    })


def downgrade() -> None:
    pass
