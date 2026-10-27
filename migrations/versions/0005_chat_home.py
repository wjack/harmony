"""The chat is home: one answer for a question several conversations ask.

- `consults.same_as_id`: the earlier consult on the request this one asks again
  (Claude's match), always the group's first, the card.
- `consults.goes_to_org_ids`: on a card, the organizations its answer goes to.
- `consults.drafted_from_id`: the escalation whose member answer a reused draft
  started from.
- `escalation_drafts.held_reason`: why a reused rev waits for a person.

Additive, with no backfill. Generated from `harmony.db.models`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("consults", sa.Column("same_as_id", sa.UUID(), nullable=True))
    op.add_column(
        "consults",
        sa.Column(
            "goes_to_org_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("consults", sa.Column("drafted_from_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_consults_world_id_same_as_id_consults"),
        "consults",
        "consults",
        ["world_id", "same_as_id"],
        ["world_id", "id"],
        initially="DEFERRED",
        deferrable=True,
    )
    op.create_foreign_key(
        op.f("fk_consults_world_id_drafted_from_id_escalations"),
        "consults",
        "escalations",
        ["world_id", "drafted_from_id"],
        ["world_id", "id"],
        initially="DEFERRED",
        deferrable=True,
    )
    op.add_column("escalation_drafts", sa.Column("held_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("escalation_drafts", "held_reason")
    op.drop_constraint(
        op.f("fk_consults_world_id_drafted_from_id_escalations"), "consults", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_consults_world_id_same_as_id_consults"), "consults", type_="foreignkey"
    )
    op.drop_column("consults", "drafted_from_id")
    op.drop_column("consults", "goes_to_org_ids")
    op.drop_column("consults", "same_as_id")
