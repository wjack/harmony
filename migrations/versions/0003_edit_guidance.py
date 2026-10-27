"""Edit guidance (docs/design/EDIT.md §2, docs/FEEDBACK_R2_PLAN.md §1.1).

- `steers`: a member's instruction to its side's Claude on a thread (an Edit, or a
  Reopen's text), never a comment.
- `rep_guidance`: what a side's representative follows on a thread, append-only and
  versioned per (thread, side).
- `ck_agent_turns_role` gains the `steer` role.

Additive: production has data. Generated from `harmony.db.models`; the role CHECK is
replaced by hand, since autogenerate doesn't compare CHECKs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = "'internal', 'representative', 'answerer', 'sharing_check', 'synthesizer', 'screener'"


def upgrade() -> None:
    op.create_table(
        "steers",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("author_member_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("escalation_id", sa.UUID(), nullable=True),
        sa.Column("draft_text", sa.Text(), nullable=True),
        sa.Column("draft_rev", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'working'"), nullable=False),
        sa.Column("guidance_outcome", sa.Text(), nullable=True),
        sa.Column("guidance_version", sa.Integer(), nullable=True),
        sa.Column("draft_outcome", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "draft_outcome IN ('rewritten', 'settled', 'failed')",
            name=op.f("ck_steers_draft_outcome"),
        ),
        sa.CheckConstraint(
            "failure_reason IN ('session_budget', 'daily_budget', 'demo_budget', 'paused',"
            " 'model_error', 'refused', 'added_details', 'invalid', 'thread_closed',"
            " 'unchanged', 'reopen_failed')",
            name=op.f("ck_steers_failure_reason"),
        ),
        sa.CheckConstraint(
            "guidance_outcome IN ('updated', 'unchanged', 'rejected', 'failed')",
            name=op.f("ck_steers_guidance_outcome"),
        ),
        sa.CheckConstraint("kind IN ('edit', 'reopen')", name=op.f("ck_steers_kind")),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_steers_side")),
        sa.CheckConstraint(
            "status IN ('working', 'done', 'failed')", name=op.f("ck_steers_status")
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 4000", name=op.f("ck_steers_body_length")
        ),
        sa.CheckConstraint(
            "char_length(draft_text) <= 20000", name=op.f("ck_steers_draft_text_length")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "author_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_steers_world_id_author_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "escalation_id"],
            ["escalations.world_id", "escalations.id"],
            name=op.f("fk_steers_world_id_escalation_id_escalations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_steers_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_steers_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_steers_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_steers")),
        sa.UniqueConstraint(
            "world_id",
            "thread_id",
            "side",
            "seq",
            name=op.f("uq_steers_world_id_thread_id_side_seq"),
        ),
    )
    op.create_table(
        "rep_guidance",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("steer_id", sa.UUID(), nullable=False),
        sa.Column("created_by_member_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_rep_guidance_side")),
        sa.CheckConstraint("char_length(text) <= 2000", name=op.f("ck_rep_guidance_text_length")),
        sa.ForeignKeyConstraint(
            ["world_id", "created_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_rep_guidance_world_id_created_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_rep_guidance_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "steer_id"],
            ["steers.world_id", "steers.id"],
            name=op.f("fk_rep_guidance_world_id_steer_id_steers"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_rep_guidance_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_guidance_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_rep_guidance")),
        sa.UniqueConstraint("world_id", "steer_id", name=op.f("uq_rep_guidance_world_id_steer_id")),
        sa.UniqueConstraint(
            "world_id",
            "thread_id",
            "side",
            "version",
            name=op.f("uq_rep_guidance_world_id_thread_id_side_version"),
        ),
    )
    _replace_role_check(f"{_ROLES}, 'steer'")


def downgrade() -> None:
    _replace_role_check(_ROLES)
    op.drop_table("rep_guidance")
    op.drop_table("steers")


def _replace_role_check(roles: str) -> None:
    op.drop_constraint(op.f("ck_agent_turns_role"), "agent_turns", type_="check")
    op.create_check_constraint(op.f("ck_agent_turns_role"), "agent_turns", f"role IN ({roles})")
