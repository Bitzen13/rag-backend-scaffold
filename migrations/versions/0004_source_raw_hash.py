"""Record a hash of each source's raw bytes, so an unchanged file is never opened.

`content_hash` is a hash of the extracted text, so computing it costs a full parse. Phase 6 skips
unchanged sources before extraction, which needs a signal that can be had without opening the
file: a hash of its raw bytes.

Existing rows get an empty hash, which matches no file, so each source is opened once on the next
run and gets its raw hash recorded then.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("source_hash", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("sources", "source_hash")
