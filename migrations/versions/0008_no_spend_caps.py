"""No in-app spend caps: the Anthropic Console's spend limit is the hosted demo's backstop.

- `budgets` and `budget_reservations` go, with `worlds.budget_lineage`,
  `sessions.budget_lineage` and `sessions.client_key_hash`, which named their rows;
- the `session_budget` and `daily_budget` failure reasons go: rows that have them
  become `demo_budget`. `live_controls` (the live switch) stays.

Downgrading puts the tables back empty, gives each world and session a lineage of its
own, and leaves `client_key_hash` unknown; reasons stay `demo_budget`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GONE = "('session_budget', 'daily_budget')"
_REASONS = "'demo_budget', 'paused', 'model_error', 'refused'"
_OLD_REASONS = "'session_budget', 'daily_budget', " + _REASONS
_STEER_ONLY = "'added_details', 'invalid', 'thread_closed', 'unchanged', 'reopen_failed'"
# (table, column, constraint, steer-only reasons too)
_CHECKS = (
    ("threads", "stop_reason", "ck_threads_stop_reason", False),
    ("thread_runs", "stop_reason", "ck_thread_runs_stop_reason", False),
    ("steers", "failure_reason", "ck_steers_failure_reason", True),
)


def _check(column: str, reasons: str, steer: bool) -> str:
    return f"{column} IN ({reasons}{', ' + _STEER_ONLY if steer else ''})"


def upgrade() -> None:
    for table, column, name, steer in _CHECKS:
        op.execute(f"UPDATE {table} SET {column} = 'demo_budget' WHERE {column} IN {_GONE}")
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, _check(column, _REASONS, steer))

    op.drop_column("sessions", "client_key_hash")
    op.drop_column("sessions", "budget_lineage")
    op.drop_column("worlds", "budget_lineage")
    op.drop_table("budget_reservations")
    op.drop_table("budgets")


def downgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("cap_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column(
            "spent_usd",
            sa.Numeric(precision=14, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reserved_usd",
            sa.Numeric(precision=14, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "spent_usd >= 0 AND reserved_usd >= 0", name=op.f("ck_budgets_non_negative")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_budgets")),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("keys", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("est_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'reserved'"), nullable=False),
        sa.Column("settled_usd", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'released')",
            name=op.f("ck_budget_reservations_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservations")),
    )
    for table in ("worlds", "sessions"):
        op.add_column(
            table,
            sa.Column(
                "budget_lineage",
                sa.UUID(),
                server_default=sa.text("gen_random_uuid()"),
                nullable=False,
            ),
        )
        op.alter_column(table, "budget_lineage", server_default=None)
    op.add_column("sessions", sa.Column("client_key_hash", sa.Text(), nullable=True))

    for table, column, name, steer in _CHECKS:
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, _check(column, _OLD_REASONS, steer))
