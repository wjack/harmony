"""The schema's structural rules, checked on the ORM metadata (ARCHITECTURE.md §4)."""

from __future__ import annotations

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql

from harmony.db.models import SNAPSHOT_ENTITIES, Base, Versioned

TABLES = Base.metadata.tables

# Every table §4 names, as REVISION_UNSTRUCTURED.md §8 revises it (the tables of the
# removed concepts dropped, trusted organizations added), plus world_workflows and the
# P1 additions (cards, rep_transcript) and the live switch (live_controls). The set is
# exact, so nothing removed is left.
EXPECTED_TABLES = {
    "sessions",
    "worlds",
    "orgs",
    "rep_settings",
    "rep_policy",
    "screenings",
    "communities",
    "org_communities",
    "members",
    "trusted_organizations",
    "knowledge_items",
    "documents",
    "conversations",
    "chat_messages",
    "network_requests",
    "threads",
    "thread_messages",
    "thread_access",
    "internal_notes",
    "rep_sessions",
    "rep_notices",
    "agent_turns",
    "model_calls",
    "consults",
    "escalations",
    "escalation_drafts",
    "reviews",
    "events",
    "world_event_seq",
    "budgets",
    "budget_reservations",
    "live_controls",
    "world_workflows",
    "cards",
    "rep_transcript",
    "steers",
    "rep_guidance",
    "thread_runs",
}

# model_calls is keyed by the globally unique turn: `call_model_checkpointed` looks
# a call up by (turn_id, step_idx) alone. It still carries world_id for cleanup.
KEYED_BY_TURN = {"model_calls"}
NOT_WORLD_SCOPED = {"worlds", "sessions", "budgets", "budget_reservations", "live_controls"}
WORLD_SCOPED = {name for name, table in TABLES.items() if "world_id" in table.c} - KEYED_BY_TURN


def _pk(table: str) -> list[str]:
    return [column.name for column in TABLES[table].primary_key.columns]


def _fks(table: Table) -> list[ForeignKeyConstraint]:
    return [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]


def test_every_section_4_table_exists_and_the_dropped_shape_does_not() -> None:
    assert set(TABLES) == EXPECTED_TABLES
    assert not {"routing_rules", "capabilities", "proposals", "intents"} & set(TABLES)


def test_a_trusted_organization_is_one_row_per_counterparty_never_the_org_itself() -> None:
    table = TABLES["trusted_organizations"]
    assert _pk("trusted_organizations") == ["world_id", "org_id", "counterparty_org_id"]
    assert table.c.autonomy.nullable
    checks = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "org_id <> counterparty_org_id" in checks


def test_world_scoping_covers_every_table_but_the_global_ones() -> None:
    assert set(TABLES) - WORLD_SCOPED == NOT_WORLD_SCOPED | KEYED_BY_TURN


@pytest.mark.parametrize("table", sorted(WORLD_SCOPED))
def test_world_scoped_primary_keys_start_with_world_id(table: str) -> None:
    assert _pk(table)[0] == "world_id"


@pytest.mark.parametrize("table", sorted(WORLD_SCOPED | KEYED_BY_TURN))
def test_world_id_references_the_world(table: str) -> None:
    targets = {
        fk.elements[0].target_fullname
        for fk in _fks(TABLES[table])
        if list(fk.columns.keys()) == ["world_id"]
    }
    # events hang off their world's counter row (see harmony.db.models.event).
    expected = "world_event_seq.world_id" if table == "events" else "worlds.id"
    assert targets == {expected}


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_foreign_key_to_a_world_scoped_table_includes_world_id(table: str) -> None:
    for fk in _fks(TABLES[table]):
        referred = fk.referred_table.name
        if referred not in WORLD_SCOPED:
            continue
        pairs = {e.column.name: e.parent.name for e in fk.elements}
        assert "world_id" in pairs, f"{fk.name} omits {referred}.world_id"
        if table in WORLD_SCOPED:
            # Same world on both sides: the referencing row's own world_id.
            assert pairs["world_id"] == "world_id", fk.name


def test_events_are_keyed_by_world_and_seq_on_a_separate_counter() -> None:
    assert _pk("events") == ["world_id", "seq"]
    assert _pk("world_event_seq") == ["world_id"]
    assert "seq" in TABLES["world_event_seq"].c
    assert "seq" not in TABLES["worlds"].c
    assert isinstance(TABLES["events"].c.payload.type, postgresql.JSONB)
    assert {"scope_kind", "org_id", "member_id", "thread_id", "session_id"} <= set(
        TABLES["events"].c.keys()
    )


def test_model_calls_are_keyed_by_turn_and_step_and_store_raw_text() -> None:
    assert _pk("model_calls") == ["turn_id", "step_idx"]
    for column in ("request_text", "response_text"):
        assert isinstance(TABLES["model_calls"].c[column].type, Text)
        assert not isinstance(TABLES["model_calls"].c[column].type, postgresql.JSONB)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("chat_messages", "content_text"),
        ("rep_transcript", "content_text"),
        ("rep_sessions", "system_text"),
        ("rep_sessions", "tools_text"),
    ],
)
def test_transcript_bytes_are_text(table: str, column: str) -> None:
    assert type(TABLES[table].c[column].type) is Text


@pytest.mark.parametrize("table", sorted(NOT_WORLD_SCOPED))
def test_global_tables_carry_no_world_id_in_their_key(table: str) -> None:
    assert not any("world" in name for name in _pk(table))


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("thread_messages", ("world_id", "thread_id", "seq")),
        ("chat_messages", ("world_id", "conversation_id", "seq")),
        ("escalations", ("world_id", "dedupe_key")),
        ("consults", ("world_id", "dedupe_key")),
        ("steers", ("world_id", "thread_id", "side", "seq")),
        ("rep_guidance", ("world_id", "thread_id", "side", "version")),
        ("rep_guidance", ("world_id", "steer_id")),
    ],
)
def test_sequences_and_dedupe_keys_are_unique(table: str, columns: tuple[str, ...]) -> None:
    uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in TABLES[table].constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert columns in uniques


def test_snapshot_entities() -> None:
    assert set(SNAPSHOT_ENTITIES) == {
        "org",
        "member",
        "rep_settings",
        "rep_policy",
        "trusted_organization",
        "knowledge_item",
        "document",
        "conversation",
        "card",
        "network_request",
        "thread",
        "consult",
        "escalation",
        "steer",
    }


@pytest.mark.parametrize("entity_type", sorted(SNAPSHOT_ENTITIES))
def test_every_snapshot_entity_has_a_version(entity_type: str) -> None:
    model = SNAPSHOT_ENTITIES[entity_type]
    mapper = sa_inspect(model, raiseerr=True)
    version = mapper.local_table.c.version
    assert isinstance(version.type, Integer)
    assert not version.nullable
    if issubclass(model, Versioned):
        # Bumped by the ORM on every update (optimistic concurrency).
        assert mapper.version_id_col is version
    else:
        # Immutable versioned rows: the version is part of the key.
        assert version.primary_key


@pytest.mark.parametrize("entity_type", sorted(SNAPSHOT_ENTITIES))
def test_snapshot_omits_name_real_columns(entity_type: str) -> None:
    model = SNAPSHOT_ENTITIES[entity_type]
    assert model.snapshot_omits <= set(sa_inspect(model, raiseerr=True).columns.keys())


def test_a_steer_snapshot_leaves_out_the_members_unsaved_draft() -> None:
    assert SNAPSHOT_ENTITIES["steer"].snapshot_omits == {"draft_text"}


def test_foreign_keys_are_checked_at_commit_except_the_outbox_insert() -> None:
    immediate = {
        fk.name
        for table in TABLES.values()
        for fk in _fks(table)
        if not (fk.deferrable and (fk.initially or "").upper() == "DEFERRED")
    }
    assert immediate == {"fk_events_world_id_world_event_seq"}


def test_every_constraint_and_index_name_fits_postgres() -> None:
    names = [
        str(item.name) for table in TABLES.values() for item in [*table.constraints, *table.indexes]
    ]
    assert all(len(name) <= 63 for name in names), [n for n in names if len(n) > 63]
    assert len(names) == len(set(names))
