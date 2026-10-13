"""document pages and full-text index

Revision ID: 003
Revises: 001
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from openfoia.doctext import backfill_single_pages, ensure_fts

    op.create_table(
        "document_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("document_id", "page_number"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])

    conn = op.get_bind()
    backfill_single_pages(conn)
    # No FTS5 in this SQLite build (some SQLCipher builds): grep falls back to LIKE.
    ensure_fts(conn)


def downgrade() -> None:
    for trigger in ("document_pages_ai", "document_pages_ad", "document_pages_au"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS document_pages_fts")
    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_table("document_pages")
