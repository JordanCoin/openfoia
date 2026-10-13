"""per-page extraction verdict

Revision ID: 005
Revises: 003
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_pages", sa.Column("verdict", sa.String(32), nullable=True))


def downgrade() -> None:
    from openfoia.doctext import ensure_fts

    with op.batch_alter_table("document_pages") as batch:
        batch.drop_column("verdict")
    # batch mode rebuilds the table, which drops the FTS sync triggers
    ensure_fts(op.get_bind())
