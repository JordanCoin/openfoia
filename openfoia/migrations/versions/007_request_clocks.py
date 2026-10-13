"""requests: lifecycle clocks (extension, tolling, denial, appeal, still-interested)

Revision ID: 007
Revises: 006
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    "extended_at",
    "tolled_until",
    "denied_at",
    "appeal_due_at",
    "appealed_at",
    "still_interested_due_at",
)


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("requests", sa.Column(name, sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("requests") as batch:
        for name in reversed(COLUMNS):
            batch.drop_column(name)
