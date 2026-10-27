"""The live demo (LIVE_DEMO_PLAN 3.3-3.5, 3.10 and 3.13).

- `sessions`: the superseded `access_code_hash` goes; `client_key_hash` records the
  client key a session was created from, for its `client:` budget row.
- `live_controls`: the live switch, one row keyed `live`, inserted here.
- `threads.stop_reason`: why the system stopped a thread (a `FailureReason`).
- Titles and asks: `network_requests.title`, `threads.title` and
  `escalation_drafts.short_question`.
- Send mode: `rep_policy.send_without_confirmation` (the org default),
  `conversations.send_without_confirmation` (a chat's override) and `cards.auto`.

Generated from `harmony.db.models`; the CHECK on `stop_reason` and the `live` row are
added by hand, since autogenerate sees neither.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("sessions", "access_code_hash")
    op.add_column("sessions", sa.Column("client_key_hash", sa.Text(), nullable=True))

    op.create_table(
        "live_controls",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("provider_blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_live_controls")),
    )
    op.execute("INSERT INTO live_controls (key) VALUES ('live')")

    op.add_column("threads", sa.Column("stop_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_threads_stop_reason"),
        "threads",
        "stop_reason IN ('session_budget', 'daily_budget', 'demo_budget', 'paused', "
        "'model_error', 'refused')",
    )

    op.add_column("network_requests", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("threads", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("escalation_drafts", sa.Column("short_question", sa.Text(), nullable=True))

    op.add_column(
        "rep_policy",
        sa.Column(
            "send_without_confirmation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "conversations", sa.Column("send_without_confirmation", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "cards", sa.Column("auto", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("cards", "auto")
    op.drop_column("conversations", "send_without_confirmation")
    op.drop_column("rep_policy", "send_without_confirmation")

    op.drop_column("escalation_drafts", "short_question")
    op.drop_column("threads", "title")
    op.drop_column("network_requests", "title")

    op.drop_constraint(op.f("ck_threads_stop_reason"), "threads", type_="check")
    op.drop_column("threads", "stop_reason")

    op.drop_table("live_controls")

    op.drop_column("sessions", "client_key_hash")
    op.add_column("sessions", sa.Column("access_code_hash", sa.Text(), nullable=True))
