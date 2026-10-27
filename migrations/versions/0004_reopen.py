"""Reopen (docs/design/REOPEN.md §7.1, docs/FEEDBACK_R2_PLAN.md §1.1).

- `threads`: the current `run`; how it ended (`end_reason`, `ended_at`, `closed_by`);
  and `run_finished_at`, which gates Reopen. No CHECK ties `end_reason` to `state`:
  terminal rows written before this revision have it NULL.
- `thread_runs`: one row per run that started, with what it started with, how it ended,
  each side's archived close summary and the carry the next run resumes from.
- `network_requests`: `round` and `workflow_done_at`.

Additive, with no backfill: worlds from an older build are forked fresh on the next
visit. Generated from `harmony.db.models`; the CHECKs on the new `threads` columns are
added by hand, since autogenerate doesn't see them.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_END_REASONS = "'closed', 'quiet', 'unsent', 'turn_limit', 'deadline', 'stopped'"
_SIDES = "'requester', 'responder'"
_REOPENED = "reopened_by_member_id, reopened_by_org_id, reopened_side, steer_id"


def upgrade() -> None:
    op.add_column(
        "threads", sa.Column("run", sa.Integer(), server_default=sa.text("1"), nullable=False)
    )
    op.add_column("threads", sa.Column("end_reason", sa.Text(), nullable=True))
    op.add_column("threads", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("threads", sa.Column("closed_by", sa.Text(), nullable=True))
    op.add_column(
        "threads", sa.Column("run_finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_threads_end_reason"), "threads", f"end_reason IN ({_END_REASONS})"
    )
    op.create_check_constraint(op.f("ck_threads_closed_by"), "threads", f"closed_by IN ({_SIDES})")

    op.create_table(
        "thread_runs",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("run", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("reopened_by_member_id", sa.UUID(), nullable=True),
        sa.Column("reopened_by_org_id", sa.UUID(), nullable=True),
        sa.Column("reopened_side", sa.Text(), nullable=True),
        sa.Column("steer_id", sa.UUID(), nullable=True),
        sa.Column("start_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("closed_by", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requester_summary", sa.Text(), nullable=True),
        sa.Column("responder_summary", sa.Text(), nullable=True),
        sa.Column("carry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"closed_by IN ({_SIDES})", name=op.f("ck_thread_runs_closed_by")),
        sa.CheckConstraint(
            f"end_reason IN ({_END_REASONS})", name=op.f("ck_thread_runs_end_reason")
        ),
        sa.CheckConstraint(
            f"reopened_side IN ({_SIDES})", name=op.f("ck_thread_runs_reopened_side")
        ),
        sa.CheckConstraint(
            "state IN ('active', 'awaiting_principal', 'closed', 'expired')",
            name=op.f("ck_thread_runs_state"),
        ),
        sa.CheckConstraint(
            "stop_reason IN ('session_budget', 'daily_budget', 'demo_budget', 'paused',"
            " 'model_error', 'refused')",
            name=op.f("ck_thread_runs_stop_reason"),
        ),
        sa.CheckConstraint(
            f"(run = 1 AND num_nonnulls({_REOPENED}) = 0)"
            f" OR (run > 1 AND num_nulls({_REOPENED}) = 0)",
            name=op.f("ck_thread_runs_reopened"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "reopened_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_thread_runs_world_id_reopened_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "reopened_by_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_thread_runs_world_id_reopened_by_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "steer_id"],
            ["steers.world_id", "steers.id"],
            name=op.f("fk_thread_runs_world_id_steer_id_steers"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_thread_runs_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_thread_runs_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "thread_id", "run", name=op.f("pk_thread_runs")),
    )

    op.add_column(
        "network_requests",
        sa.Column("round", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "network_requests",
        sa.Column("workflow_done_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("network_requests", "workflow_done_at")
    op.drop_column("network_requests", "round")

    op.drop_table("thread_runs")

    op.drop_constraint(op.f("ck_threads_closed_by"), "threads", type_="check")
    op.drop_constraint(op.f("ck_threads_end_reason"), "threads", type_="check")
    op.drop_column("threads", "run_finished_at")
    op.drop_column("threads", "closed_by")
    op.drop_column("threads", "ended_at")
    op.drop_column("threads", "end_reason")
    op.drop_column("threads", "run")
