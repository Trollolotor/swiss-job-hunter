"""Add structured CV and profile defaults."""
from alembic import op
from sqlalchemy import inspect

revision = "20260622_profile_cv"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # This is the first Alembic revision; create any tables previously managed
    # by the idempotent startup bootstrap before applying column upgrades.
    from db.models import Base
    Base.metadata.create_all(bind=bind)
    job_columns = {column["name"] for column in inspect(bind).get_columns("jobs")}
    if "posted_at_source" not in job_columns:
        op.execute("ALTER TABLE jobs ADD COLUMN posted_at_source VARCHAR(30) DEFAULT 'unknown'")
    columns = {column["name"] for column in inspect(bind).get_columns("search_profiles")}
    additions = {
        "cv_sections_json": "TEXT DEFAULT '{}'",
        "default_location": "VARCHAR(200) DEFAULT 'Zürich'",
        "parent_profile_id": "INTEGER",
        "tailored_for_job_id": "INTEGER",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            op.execute(f"ALTER TABLE search_profiles ADD COLUMN {name} {sql_type}")


def downgrade() -> None:
    pass
