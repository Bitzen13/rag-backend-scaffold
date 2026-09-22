"""Track ingestion runs triggered over the internal admin path.

Ingestion takes minutes, so the admin route starts it and answers 202 rather than holding the
request open (code-standards.md: long-running work belongs outside request handlers). The
operator then polls for the outcome, which means the run's state has to outlive the request that
started it, and be visible from whichever worker serves the poll. Hence a table rather than
process memory.

The partial unique index is what makes "one run at a time" true across workers: a second claim
while one is running violates it and is refused, rather than two runs racing each other over the
same documents.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
_NEW_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=_NEW_UUID, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "report", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed', 'abandoned')",
            name="ck_ingestion_jobs_state",
        ),
    )
    # At most one running job, enforced by Postgres rather than by a check inside one process.
    op.create_index(
        "uq_ingestion_jobs_one_running",
        "ingestion_jobs",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index("ix_ingestion_jobs_started_at", "ingestion_jobs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_started_at", table_name="ingestion_jobs")
    op.drop_index("uq_ingestion_jobs_one_running", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
