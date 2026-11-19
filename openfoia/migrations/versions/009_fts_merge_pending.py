"""fts merge pending: record a full-text merge owed after page text is deleted

Revision ID: 009
Revises: 008
Create Date: 2026-11-19

The FTS5 index keeps a deleted page's words in older segments until it is
merged. The merge runs after the session that deleted the text; this one-row
table records that it is owed, in the deleting transaction, so a merge that
fails (database busy) or never runs (process killed) is retried by the next
session. Like the FTS5 index itself it is not an ORM model.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database migrated from before 009 may hold deleted words already; the
    # first row makes the next session merge the index once.
    op.execute(
        "CREATE TABLE IF NOT EXISTS fts_merge_pending (id INTEGER PRIMARY KEY CHECK (id = 1))"
    )
    op.execute("INSERT OR IGNORE INTO fts_merge_pending (id) VALUES (1)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fts_merge_pending")
