"""Initial schema.

Every table in ARCHITECTURE.md §4, as revised for unstructured agents
(REVISION_UNSTRUCTURED.md §8), plus world_workflows.

Generated from `harmony.db.models` and reviewed, with no hand edits. Foreign keys are
DEFERRABLE INITIALLY DEFERRED (see `harmony.db.models.base.DEFERRED`), except
events -> world_event_seq.

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        "worlds",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'live'"), nullable=False),
        sa.Column("build_sha", sa.Text(), nullable=False),
        sa.Column("budget_lineage", sa.UUID(), nullable=False),
        sa.Column("ff_offset_s", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('live', 'replay')", name=op.f("ck_worlds_mode")),
        sa.CheckConstraint(
            "status IN ('live', 'retiring', 'retired')", name=op.f("ck_worlds_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worlds")),
    )
    op.create_table(
        "communities",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_communities_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_communities")),
        sa.UniqueConstraint("world_id", "slug", name=op.f("uq_communities_world_id_slug")),
    )
    op.create_table(
        "model_calls",
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("step_idx", sa.Integer(), nullable=False),
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("served_model", sa.Text(), nullable=False),
        sa.Column("usage_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_model_calls_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("turn_id", "step_idx", name=op.f("pk_model_calls")),
    )
    op.create_index(op.f("ix_model_calls_world_id"), "model_calls", ["world_id"], unique=False)
    op.create_table(
        "orgs",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "current_settings_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_orgs_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_orgs")),
        sa.UniqueConstraint("world_id", "slug", name=op.f("uq_orgs_world_id_slug")),
    )
    op.create_table(
        "world_event_seq",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_world_event_seq_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", name=op.f("pk_world_event_seq")),
    )
    op.create_table(
        "world_workflows",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("workflow_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_world_workflows_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "workflow_id", name=op.f("pk_world_workflows")),
    )
    op.create_table(
        "events",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=True),
        sa.Column("member_id", sa.UUID(), nullable=True),
        sa.Column("thread_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("entity_version", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_kind IN ('world', 'org', 'member', 'thread', 'demo', 'session')",
            name=op.f("ck_events_scope_kind"),
        ),
        sa.CheckConstraint(
            "num_nulls(entity_type, entity_id, entity_version) IN (0, 3)",
            name=op.f("ck_events_entity_columns"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["world_event_seq.world_id"],
            name=op.f("fk_events_world_id_world_event_seq"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("world_id", "seq", name=op.f("pk_events")),
    )
    op.create_table(
        "members",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name=op.f("ck_members_role")),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_members_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_members_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_members")),
        sa.UniqueConstraint("world_id", "slug", name=op.f("uq_members_world_id_slug")),
    )
    op.create_index(
        op.f("ix_members_world_id_org_id"), "members", ["world_id", "org_id"], unique=False
    )
    op.create_table(
        "org_communities",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("community_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "community_id"],
            ["communities.world_id", "communities.id"],
            name=op.f("fk_org_communities_world_id_community_id_communities"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_org_communities_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_org_communities_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "community_id", "org_id", name=op.f("pk_org_communities")
        ),
    )
    op.create_index(
        op.f("ix_org_communities_world_id_org_id"),
        "org_communities",
        ["world_id", "org_id"],
        unique=False,
    )
    op.create_table(
        "reviews",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("about_org_id", sa.UUID(), nullable=False),
        sa.Column("by_org_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_reviews_rating_range")),
        sa.ForeignKeyConstraint(
            ["world_id", "about_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_reviews_world_id_about_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "by_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_reviews_world_id_by_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_reviews_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_reviews")),
    )
    op.create_index(
        op.f("ix_reviews_world_id_about_org_id"),
        "reviews",
        ["world_id", "about_org_id"],
        unique=False,
    )
    op.create_table(
        "screenings",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("search_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("requester_org_id", sa.UUID(), nullable=False),
        sa.Column("fit", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fit IN ('strong', 'possible', 'no', 'unknown')", name=op.f("ck_screenings_fit")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_screenings_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "requester_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_screenings_world_id_requester_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_screenings_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "search_id", "org_id", name=op.f("pk_screenings")),
    )
    op.create_table(
        "conversations",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("owner_member_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_conversations_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "owner_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_conversations_world_id_owner_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_conversations_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_conversations")),
    )
    op.create_index(
        op.f("ix_conversations_world_id_owner_member_id"),
        "conversations",
        ["world_id", "owner_member_id"],
        unique=False,
    )
    op.create_table(
        "documents",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("object_name", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("uploaded_by_member_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_documents_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "uploaded_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_documents_world_id_uploaded_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_documents_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "world_id", "org_id", "slug", name=op.f("uq_documents_world_id_org_id_slug")
        ),
    )
    op.create_index(
        op.f("ix_documents_world_id_org_id"), "documents", ["world_id", "org_id"], unique=False
    )
    op.create_table(
        "rep_policy",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("autonomy", sa.Text(), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("sharing_description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "directory_visibility", sa.Text(), server_default=sa.text("'public'"), nullable=False
        ),
        sa.Column("routing_prompt", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("default_assignee_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "autonomy IN ('ask', 'auto', 'skip')", name=op.f("ck_rep_policy_autonomy")
        ),
        sa.CheckConstraint(
            "directory_visibility IN ('public', 'communities', 'unlisted')",
            name=op.f("ck_rep_policy_directory_visibility"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "default_assignee_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_rep_policy_world_id_default_assignee_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_rep_policy_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_policy_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "org_id", name=op.f("pk_rep_policy")),
    )
    op.create_table(
        "rep_settings",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("public_summary", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("created_by_member_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "created_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_rep_settings_world_id_created_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_rep_settings_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_settings_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "org_id", "version", name=op.f("pk_rep_settings")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("desk_world_id", sa.UUID(), nullable=True),
        sa.Column("desk_member_id", sa.UUID(), nullable=True),
        sa.Column("network_world_id", sa.UUID(), nullable=True),
        sa.Column("network_member_id", sa.UUID(), nullable=True),
        sa.Column("epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("budget_lineage", sa.UUID(), nullable=False),
        sa.Column("access_code_hash", sa.Text(), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["desk_world_id", "desk_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_sessions_desk_world_id_desk_member_id_members"),
            ondelete="SET NULL",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["desk_world_id"],
            ["worlds.id"],
            name=op.f("fk_sessions_desk_world_id_worlds"),
            ondelete="SET NULL",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["network_world_id", "network_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_sessions_network_world_id_network_member_id_members"),
            ondelete="SET NULL",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["network_world_id"],
            ["worlds.id"],
            name=op.f("fk_sessions_network_world_id_worlds"),
            ondelete="SET NULL",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(op.f("ix_sessions_desk_world_id"), "sessions", ["desk_world_id"], unique=False)
    op.create_index(
        op.f("ix_sessions_network_world_id"), "sessions", ["network_world_id"], unique=False
    )
    op.create_table(
        "trusted_organizations",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("counterparty_org_id", sa.UUID(), nullable=False),
        sa.Column("autonomy", sa.Text(), nullable=True),
        sa.Column("sharing_description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("updated_by_member_id", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "autonomy IN ('ask', 'auto', 'skip')", name=op.f("ck_trusted_organizations_autonomy")
        ),
        sa.CheckConstraint(
            "org_id <> counterparty_org_id", name=op.f("ck_trusted_organizations_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "counterparty_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_trusted_organizations_world_id_counterparty_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_trusted_organizations_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "updated_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_trusted_organizations_world_id_updated_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_trusted_organizations_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "org_id", "counterparty_org_id", name=op.f("pk_trusted_organizations")
        ),
    )
    op.create_table(
        "cards",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("tool_use_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("kind IN ('outreach')", name=op.f("ck_cards_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_cards_status"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "conversation_id"],
            ["conversations.world_id", "conversations.id"],
            name=op.f("fk_cards_world_id_conversation_id_conversations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_cards_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_cards")),
    )
    op.create_index(
        op.f("ix_cards_world_id_conversation_id"),
        "cards",
        ["world_id", "conversation_id"],
        unique=False,
    )
    op.create_table(
        "chat_messages",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("author_member_id", sa.UUID(), nullable=True),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name=op.f("ck_chat_messages_role")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "author_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_chat_messages_world_id_author_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "conversation_id"],
            ["conversations.world_id", "conversations.id"],
            name=op.f("fk_chat_messages_world_id_conversation_id_conversations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_chat_messages_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint(
            "world_id",
            "conversation_id",
            "seq",
            name=op.f("uq_chat_messages_world_id_conversation_id_seq"),
        ),
    )
    op.create_table(
        "knowledge_items",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), server_default=sa.text("'note'"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.UUID(), nullable=True),
        sa.Column("author_member_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id", "author_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_knowledge_items_world_id_author_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "doc_id"],
            ["documents.world_id", "documents.id"],
            name=op.f("fk_knowledge_items_world_id_doc_id_documents"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_knowledge_items_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_knowledge_items_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_knowledge_items")),
        sa.UniqueConstraint(
            "world_id", "org_id", "slug", name=op.f("uq_knowledge_items_world_id_org_id_slug")
        ),
    )
    op.create_index(
        op.f("ix_knowledge_items_world_id_org_id"),
        "knowledge_items",
        ["world_id", "org_id"],
        unique=False,
    )
    op.create_table(
        "network_requests",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("requester_org_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("created_by_member_id", sa.UUID(), nullable=True),
        sa.Column("card_id", sa.UUID(), nullable=True),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column(
            "recipients",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["world_id", "card_id"],
            ["cards.world_id", "cards.id"],
            name=op.f("fk_network_requests_world_id_card_id_cards"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "conversation_id"],
            ["conversations.world_id", "conversations.id"],
            name=op.f("fk_network_requests_world_id_conversation_id_conversations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "created_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_network_requests_world_id_created_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "requester_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_network_requests_world_id_requester_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_network_requests_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_network_requests")),
    )
    op.create_index(
        op.f("ix_network_requests_world_id_requester_org_id"),
        "network_requests",
        ["world_id", "requester_org_id"],
        unique=False,
    )
    op.create_table(
        "threads",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("requester_org_id", sa.UUID(), nullable=False),
        sa.Column("responder_org_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("side_to_move", sa.Text(), nullable=True),
        sa.Column("requester_settings_version", sa.Integer(), nullable=True),
        sa.Column("responder_settings_version", sa.Integer(), nullable=True),
        sa.Column("turn_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "side_to_move IN ('requester', 'responder')", name=op.f("ck_threads_side_to_move")
        ),
        sa.CheckConstraint(
            "state IN ('active', 'awaiting_principal', 'closed', 'expired')",
            name=op.f("ck_threads_state"),
        ),
        sa.CheckConstraint(
            "requester_org_id <> responder_org_id", name=op.f("ck_threads_bilateral")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "request_id"],
            ["network_requests.world_id", "network_requests.id"],
            name=op.f("fk_threads_world_id_request_id_network_requests"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "requester_org_id", "requester_settings_version"],
            ["rep_settings.world_id", "rep_settings.org_id", "rep_settings.version"],
            name="fk_threads_requester_settings",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "requester_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_threads_world_id_requester_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "responder_org_id", "responder_settings_version"],
            ["rep_settings.world_id", "rep_settings.org_id", "rep_settings.version"],
            name="fk_threads_responder_settings",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "responder_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_threads_world_id_responder_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_threads_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_threads")),
    )
    op.create_index(
        op.f("ix_threads_world_id_request_id"), "threads", ["world_id", "request_id"], unique=False
    )
    op.create_index(
        op.f("ix_threads_world_id_requester_org_id"),
        "threads",
        ["world_id", "requester_org_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_threads_world_id_responder_org_id"),
        "threads",
        ["world_id", "responder_org_id"],
        unique=False,
    )
    op.create_table(
        "agent_turns",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=14, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('internal', 'representative', 'answerer', 'sharing_check',"
            " 'synthesizer', 'screener')",
            name=op.f("ck_agent_turns_role"),
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_agent_turns_side")),
        sa.ForeignKeyConstraint(
            ["world_id", "conversation_id"],
            ["conversations.world_id", "conversations.id"],
            name=op.f("fk_agent_turns_world_id_conversation_id_conversations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_agent_turns_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_agent_turns_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_agent_turns_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_agent_turns")),
    )
    op.create_index(
        op.f("ix_agent_turns_world_id_thread_id"),
        "agent_turns",
        ["world_id", "thread_id"],
        unique=False,
    )
    op.create_table(
        "consults",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'answering'"), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column("tool_use_id", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=True),
        sa.Column("needs_human_reason", sa.Text(), nullable=True),
        sa.Column("route", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("answer_turn_id", sa.UUID(), nullable=True),
        sa.Column("sharing_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("released_text", sa.Text(), nullable=True),
        sa.Column("released_by", sa.Text(), nullable=True),
        sa.Column("released_by_member_id", sa.UUID(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "coalesce(released_by = 'member', false) = (released_by_member_id IS NOT NULL)",
            name=op.f("ck_consults_released_by_member"),
        ),
        sa.CheckConstraint(
            "released_by IN ('auto', 'member')", name=op.f("ck_consults_released_by")
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_consults_side")),
        sa.CheckConstraint(
            "status IN ('answering', 'released', 'needs_human', 'declined')",
            name=op.f("ck_consults_status"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_consults_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "released_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_consults_world_id_released_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_consults_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_consults_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_consults")),
        sa.UniqueConstraint("world_id", "dedupe_key", name=op.f("uq_consults_world_id_dedupe_key")),
    )
    op.create_index(
        op.f("ix_consults_world_id_thread_id"), "consults", ["world_id", "thread_id"], unique=False
    )
    op.create_table(
        "internal_notes",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_member_id", sa.UUID(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "mentions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(author_kind = 'member') = (author_member_id IS NOT NULL)",
            name=op.f("ck_internal_notes_author_member"),
        ),
        sa.CheckConstraint(
            "author_kind IN ('member', 'claude')", name=op.f("ck_internal_notes_author_kind")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "author_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_internal_notes_world_id_author_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_internal_notes_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_internal_notes_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_internal_notes_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_internal_notes")),
        sa.UniqueConstraint(
            "world_id",
            "thread_id",
            "org_id",
            "seq",
            name=op.f("uq_internal_notes_world_id_thread_id_org_id_seq"),
        ),
    )
    op.create_table(
        "rep_notices",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("flushed_turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_rep_notices_side")),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_rep_notices_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_notices_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "thread_id", "side", "seq", name=op.f("pk_rep_notices")
        ),
    )
    op.create_table(
        "rep_sessions",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("system_text", sa.Text(), nullable=False),
        sa.Column("tools_text", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("close_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_rep_sessions_side")),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_rep_sessions_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_sessions_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "thread_id", "side", name=op.f("pk_rep_sessions")),
    )
    op.create_table(
        "rep_transcript",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name=op.f("ck_rep_transcript_role")
        ),
        sa.CheckConstraint(
            "side IN ('requester', 'responder')", name=op.f("ck_rep_transcript_side")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_rep_transcript_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_rep_transcript_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "thread_id", "side", "seq", name=op.f("pk_rep_transcript")
        ),
    )
    op.create_table(
        "thread_access",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("shared_by_member_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('routed', 'shared', 'admin')", name=op.f("ck_thread_access_reason")
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_thread_access_world_id_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "shared_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_thread_access_world_id_shared_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_thread_access_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_thread_access_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "thread_id", "member_id", "reason", name=op.f("pk_thread_access")
        ),
    )
    op.create_index(
        op.f("ix_thread_access_world_id_member_id"),
        "thread_access",
        ["world_id", "member_id"],
        unique=False,
    )
    op.create_table(
        "thread_messages",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_org_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("author_member_id", sa.UUID(), nullable=True),
        sa.Column("approved_by_member_id", sa.UUID(), nullable=True),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('message', 'close')", name=op.f("ck_thread_messages_kind")),
        sa.CheckConstraint(
            "provenance IN ('agent', 'agent_approved', 'human')",
            name=op.f("ck_thread_messages_provenance"),
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "approved_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_thread_messages_world_id_approved_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "author_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_thread_messages_world_id_author_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "from_org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_thread_messages_world_id_from_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_thread_messages_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_thread_messages_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_thread_messages")),
        sa.UniqueConstraint(
            "world_id", "thread_id", "seq", name=op.f("uq_thread_messages_world_id_thread_id_seq")
        ),
    )
    op.create_table(
        "escalations",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("consult_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column(
            "assignees",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("current_draft_rev", sa.Integer(), nullable=True),
        sa.Column("resolution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved_by_member_id", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint(
            "kind <> 'consult' OR consult_id IS NOT NULL",
            name=op.f("ck_escalations_subject_present"),
        ),
        sa.CheckConstraint(
            "kind IN ('consult', 'outbound_review')", name=op.f("ck_escalations_kind")
        ),
        sa.CheckConstraint("side IN ('requester', 'responder')", name=op.f("ck_escalations_side")),
        sa.ForeignKeyConstraint(
            ["world_id", "consult_id"],
            ["consults.world_id", "consults.id"],
            name=op.f("fk_escalations_world_id_consult_id_consults"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "org_id"],
            ["orgs.world_id", "orgs.id"],
            name=op.f("fk_escalations_world_id_org_id_orgs"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "resolved_by_member_id"],
            ["members.world_id", "members.id"],
            name=op.f("fk_escalations_world_id_resolved_by_member_id_members"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "thread_id"],
            ["threads.world_id", "threads.id"],
            name=op.f("fk_escalations_world_id_thread_id_threads"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_escalations_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("world_id", "id", name=op.f("pk_escalations")),
        sa.UniqueConstraint(
            "world_id", "dedupe_key", name=op.f("uq_escalations_world_id_dedupe_key")
        ),
    )
    op.create_index(
        op.f("ix_escalations_world_id_org_id_status"),
        "escalations",
        ["world_id", "org_id", "status"],
        unique=False,
    )
    op.create_table(
        "escalation_drafts",
        sa.Column("world_id", sa.UUID(), nullable=False),
        sa.Column("escalation_id", sa.UUID(), nullable=False),
        sa.Column("draft_rev", sa.Integer(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("route", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rationale", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["world_id", "escalation_id"],
            ["escalations.world_id", "escalations.id"],
            name=op.f("fk_escalation_drafts_world_id_escalation_id_escalations"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["world_id"],
            ["worlds.id"],
            name=op.f("fk_escalation_drafts_world_id_worlds"),
            ondelete="CASCADE",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint(
            "world_id", "escalation_id", "draft_rev", name=op.f("pk_escalation_drafts")
        ),
    )


def downgrade() -> None:
    op.drop_table("escalation_drafts")
    op.drop_index(op.f("ix_escalations_world_id_org_id_status"), table_name="escalations")
    op.drop_table("escalations")
    op.drop_table("thread_messages")
    op.drop_index(op.f("ix_thread_access_world_id_member_id"), table_name="thread_access")
    op.drop_table("thread_access")
    op.drop_table("rep_transcript")
    op.drop_table("rep_sessions")
    op.drop_table("rep_notices")
    op.drop_table("internal_notes")
    op.drop_index(op.f("ix_consults_world_id_thread_id"), table_name="consults")
    op.drop_table("consults")
    op.drop_index(op.f("ix_agent_turns_world_id_thread_id"), table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_index(op.f("ix_threads_world_id_responder_org_id"), table_name="threads")
    op.drop_index(op.f("ix_threads_world_id_requester_org_id"), table_name="threads")
    op.drop_index(op.f("ix_threads_world_id_request_id"), table_name="threads")
    op.drop_table("threads")
    op.drop_index(
        op.f("ix_network_requests_world_id_requester_org_id"), table_name="network_requests"
    )
    op.drop_table("network_requests")
    op.drop_index(op.f("ix_knowledge_items_world_id_org_id"), table_name="knowledge_items")
    op.drop_table("knowledge_items")
    op.drop_table("chat_messages")
    op.drop_index(op.f("ix_cards_world_id_conversation_id"), table_name="cards")
    op.drop_table("cards")
    op.drop_table("trusted_organizations")
    op.drop_index(op.f("ix_sessions_network_world_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_desk_world_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("rep_settings")
    op.drop_table("rep_policy")
    op.drop_index(op.f("ix_documents_world_id_org_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_conversations_world_id_owner_member_id"), table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("screenings")
    op.drop_index(op.f("ix_reviews_world_id_about_org_id"), table_name="reviews")
    op.drop_table("reviews")
    op.drop_index(op.f("ix_org_communities_world_id_org_id"), table_name="org_communities")
    op.drop_table("org_communities")
    op.drop_index(op.f("ix_members_world_id_org_id"), table_name="members")
    op.drop_table("members")
    op.drop_table("events")
    op.drop_table("world_workflows")
    op.drop_table("world_event_seq")
    op.drop_table("orgs")
    op.drop_index(op.f("ix_model_calls_world_id"), table_name="model_calls")
    op.drop_table("model_calls")
    op.drop_table("communities")
    op.drop_table("worlds")
    op.drop_table("budgets")
    op.drop_table("budget_reservations")
