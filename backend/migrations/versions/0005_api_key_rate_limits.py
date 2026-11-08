"""Count each API key's requests per fixed window, so several workers share one allowance.

Phase 8 limits requests per key. Production runs several Uvicorn workers, so a counter held in a
process would give each worker the full allowance and the effective limit would be the configured
one multiplied by the worker count. Counting in Postgres — which every worker already talks to —
makes the limit the limit, and needs no extra service.

The primary key is (api_key_id, window_start), so the count is a single upsert that returns the
new value, and a key's stale windows are one indexed delete away.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_key_rate_limits",
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("api_key_id", "window_start", name="pk_api_key_rate_limits"),
    )


def downgrade() -> None:
    op.drop_table("api_key_rate_limits")
