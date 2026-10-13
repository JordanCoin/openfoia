"""cases: group requests, documents and searches into an investigation

Revision ID: 002
Revises: 001
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cases_slug", "cases", ["slug"], unique=True)

    op.create_table(
        "case_queries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("total_results", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_case_queries_case_id", "case_queries", ["case_id"])

    with op.batch_alter_table("requests") as batch_op:
        batch_op.add_column(sa.Column("case_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key("fk_requests_case_id", "cases", ["case_id"], ["id"])
        batch_op.create_index("ix_requests_case_id", ["case_id"])


def downgrade() -> None:
    with op.batch_alter_table("requests") as batch_op:
        batch_op.drop_index("ix_requests_case_id")
        batch_op.drop_constraint("fk_requests_case_id", type_="foreignkey")
        batch_op.drop_column("case_id")
    op.drop_index("ix_case_queries_case_id", table_name="case_queries")
    op.drop_table("case_queries")
    op.drop_index("ix_cases_slug", table_name="cases")
    op.drop_table("cases")
