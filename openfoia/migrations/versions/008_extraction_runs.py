"""extraction runs: one row per entity-extraction pass; entities point at their run

Revision ID: 008
Revises: 007
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("entity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_extraction_runs_document_id", "extraction_runs", ["document_id"])
    # At most one current run per document, so readers never count a document twice.
    op.create_index(
        "uq_extraction_runs_current",
        "extraction_runs",
        ["document_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
    )
    # Existing entities keep extraction_run_id NULL. SQLite's ADD COLUMN accepts an
    # inline REFERENCES clause when the default is NULL, so no table rebuild is needed.
    op.execute(
        "ALTER TABLE entities ADD COLUMN extraction_run_id VARCHAR(36) "
        "REFERENCES extraction_runs (id) ON DELETE CASCADE"
    )
    op.create_index("ix_entities_extraction_run_id", "entities", ["extraction_run_id"])


def downgrade() -> None:
    op.drop_index("ix_entities_extraction_run_id", table_name="entities")
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("extraction_run_id")
    op.drop_index("uq_extraction_runs_current", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_document_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")
