"""Record the chunking settings each source was ingested with.

Phase 6 detects a changed source by its content hash; a document whose content is unchanged but
whose chunking settings changed also needs re-chunking, which is only knowable if those settings
were stored with it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_JSON = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "chunking",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=_EMPTY_JSON,
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "chunking")
