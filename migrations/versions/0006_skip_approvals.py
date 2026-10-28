"""Skip approvals: the demo switch that lets every org's Claude act without a person.

- `worlds.skip_approvals`: while true, no answer, message or request in the world waits
  for a person (`release.decide`); the sharing check still runs on every answer.

Additive, with no backfill. Generated from `harmony.db.models`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worlds",
        sa.Column("skip_approvals", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("worlds", "skip_approvals")
