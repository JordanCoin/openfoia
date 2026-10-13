"""agencies: jurisdiction id, body type, custodian title

Revision ID: 006
Revises: 005
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agencies", sa.Column("jurisdiction_id", sa.String(16), nullable=True))
    op.add_column("agencies", sa.Column("body_type", sa.String(32), nullable=True))
    op.add_column("agencies", sa.Column("custodian_title", sa.String(255), nullable=True))
    op.create_index("ix_agencies_jurisdiction_id", "agencies", ["jurisdiction_id"])
    # Every agency shipped before this revision is federal.
    op.execute(
        "UPDATE agencies SET jurisdiction_id = 'us', body_type = 'federal' "
        "WHERE jurisdiction_id IS NULL AND level = 'FEDERAL'"
    )


def downgrade() -> None:
    op.drop_index("ix_agencies_jurisdiction_id", table_name="agencies")
    with op.batch_alter_table("agencies") as batch:
        batch.drop_column("custodian_title")
        batch.drop_column("body_type")
        batch.drop_column("jurisdiction_id")
