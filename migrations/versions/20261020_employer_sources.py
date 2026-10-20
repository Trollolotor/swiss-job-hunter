"""Add employer registries, source identity, durable work, and connector evidence."""

from alembic import op
from sqlalchemy import inspect

revision = "20261020_employer_sources"
down_revision = "20260622_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    # Earlier revisions import current metadata. checkfirst is essential for
    # unversioned installations and upgrades after startup bootstrap.
    from db.models import Base

    names = {
        "employers",
        "employer_aliases",
        "employer_evidence",
        "employer_sources",
        "connector_versions",
        "source_runs",
        "job_sources",
        "job_source_revisions",
        "source_run_members",
        "employer_tasks",
        "connector_incidents",
        "employer_budget_ledger",
    }
    Base.metadata.create_all(
        op.get_bind(),
        tables=[t for t in Base.metadata.sorted_tables if t.name in names],
        checkfirst=True,
    )
    columns = {c["name"] for c in inspect(op.get_bind()).get_columns("job_profiles")}
    if "score_stale" not in columns:
        op.execute("ALTER TABLE job_profiles ADD COLUMN score_stale BOOLEAN NOT NULL DEFAULT 0")


def downgrade():
    # Operational rollback is feature-disable/application rollback, preserving evidence.
    raise RuntimeError(
        "Employer-source downgrade is destructive; restore a verified backup instead"
    )
