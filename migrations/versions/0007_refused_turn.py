"""Refused turns: a representative's turn Claude declined waits on its side's team.

- `escalations.kind` takes `refused_turn`: the thread waits for a person to Retry the
  turn or end the conversation, instead of closing;
- `escalations.turn_id`: the turn Claude declined, for a `refused_turn` item.

Additive, with no backfill. Downgrading deletes `refused_turn` items.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("turn_id", sa.UUID(), nullable=True))
    op.drop_constraint(op.f("ck_escalations_kind"), "escalations", type_="check")
    op.create_check_constraint(
        op.f("ck_escalations_kind"),
        "escalations",
        "kind IN ('consult', 'outbound_review', 'refused_turn')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM escalations WHERE kind = 'refused_turn'")
    op.drop_constraint(op.f("ck_escalations_kind"), "escalations", type_="check")
    op.create_check_constraint(
        op.f("ck_escalations_kind"), "escalations", "kind IN ('consult', 'outbound_review')"
    )
    op.drop_column("escalations", "turn_id")
