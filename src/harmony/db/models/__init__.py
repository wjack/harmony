"""ORM models, one module per aggregate (ARCHITECTURE.md §4).

Every model module is imported here so `Base.metadata` is complete for Alembic
(`migrations/env.py` imports this package) and for the schema tests.
"""

from harmony.db.models.agent import AgentTurn, ModelCall
from harmony.db.models.base import (
    Base,
    Entity,
    Snapshotable,
    Versioned,
    VersionedEntity,
    WorldScoped,
)
from harmony.db.models.conversation import Card, ChatMessage, Conversation
from harmony.db.models.escalation import Consult, Escalation, EscalationDraft
from harmony.db.models.event import Event
from harmony.db.models.knowledge import Document, KnowledgeItem
from harmony.db.models.org import (
    Community,
    Member,
    Org,
    OrgCommunity,
    RepPolicy,
    RepSettings,
    TrustedOrganization,
)
from harmony.db.models.request import NetworkRequest, Screening
from harmony.db.models.review import Review
from harmony.db.models.thread import (
    InternalNote,
    RepGuidance,
    RepNotice,
    RepSession,
    RepTranscript,
    Steer,
    Thread,
    ThreadAccess,
    ThreadMessage,
    ThreadRun,
)
from harmony.db.models.visitor import Budget, BudgetReservation, LiveControls, VisitorSession
from harmony.db.models.world import World, WorldEventSeq, WorldWorkflow


def _snapshot_entities() -> dict[str, type[Snapshotable]]:
    entities: dict[str, type[Snapshotable]] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if issubclass(cls, Snapshotable):
            if cls.entity_type in entities:
                raise RuntimeError(f"duplicate entity_type {cls.entity_type!r}")
            entities[cls.entity_type] = cls
    return entities


# `entity.snapshot` events' `entity_type` -> the model whose rows they carry.
SNAPSHOT_ENTITIES = _snapshot_entities()

__all__ = [
    "SNAPSHOT_ENTITIES",
    "AgentTurn",
    "Base",
    "Budget",
    "BudgetReservation",
    "Card",
    "ChatMessage",
    "Community",
    "Consult",
    "Conversation",
    "Document",
    "Entity",
    "Escalation",
    "EscalationDraft",
    "Event",
    "InternalNote",
    "KnowledgeItem",
    "LiveControls",
    "Member",
    "ModelCall",
    "NetworkRequest",
    "Org",
    "OrgCommunity",
    "RepGuidance",
    "RepNotice",
    "RepPolicy",
    "RepSession",
    "RepSettings",
    "RepTranscript",
    "Review",
    "Screening",
    "Snapshotable",
    "Steer",
    "Thread",
    "ThreadAccess",
    "ThreadMessage",
    "ThreadRun",
    "TrustedOrganization",
    "Versioned",
    "VersionedEntity",
    "VisitorSession",
    "World",
    "WorldEventSeq",
    "WorldScoped",
    "WorldWorkflow",
]
