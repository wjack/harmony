"""The send-mode notice: how internal Claude learns its chat's send mode changed (3.10).

The system prompt and tools never change within a chat (preserved thinking), so a
change goes out as an appended notice on the next new turn. What Claude was last told
is read from the chat's stored system messages by the notice's fixed prefix: a notice
is stored only with the call that carried it, so a retried call rebuilds the same
bytes, and no column is needed. A chat that was never told counts as reviewing first.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harmony.db import repo
from harmony.db.models import ChatMessage, Conversation, Member
from harmony.domain import conversations

NOTICE_PREFIX: Final = "Harmony: in this chat, `send_network_request` "
_SENDS_AT_ONCE: Final = NOTICE_PREFIX + "now sends at once"
SENDS_WITHOUT_REVIEW: Final = (
    _SENDS_AT_ONCE
    + ", without {member}'s review: the organizations you choose get the request as soon "
    "as you call it, and nobody checks what is asked or who is asked. Send only what "
    "{member} asked for."
)
WAITS_FOR_REVIEW: Final = NOTICE_PREFIX + "waits for {member}'s approval again."


async def send_mode_notice(
    session: AsyncSession, *, world_id: UUID, conversation_id: UUID
) -> str | None:
    """The notice for a new turn, or None when the chat's effective send mode (read
    live) is the one Claude was last told."""
    conversation = await repo.get_required(session, Conversation, world_id, conversation_id)
    effective = await conversations.effective_send_mode(session, conversation)
    if effective == await _last_told(session, conversation):
        return None
    owner = await repo.get_required(session, Member, world_id, conversation.owner_member_id)
    notice = SENDS_WITHOUT_REVIEW if effective else WAITS_FOR_REVIEW
    return notice.format(member=owner.name)


async def _last_told(session: AsyncSession, conversation: Conversation) -> bool:
    """Whether the last send-mode notice stored in the chat said it sends without
    review."""
    told = await session.scalar(
        select(ChatMessage.content_text)
        .where(
            ChatMessage.world_id == conversation.world_id,
            ChatMessage.conversation_id == conversation.id,
            ChatMessage.role == "system",
            ChatMessage.content_text.contains(NOTICE_PREFIX, autoescape=True),
        )
        .order_by(ChatMessage.seq.desc())
        .limit(1)
    )
    return told is not None and _SENDS_AT_ONCE in told


__all__ = [
    "NOTICE_PREFIX",
    "SENDS_WITHOUT_REVIEW",
    "WAITS_FOR_REVIEW",
    "send_mode_notice",
]
