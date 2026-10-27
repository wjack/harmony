"""Visitor sessions: the signed cookie and the request's acting identity.

The cookie holds only the session ID, signed with SESSION_SECRET (itsdangerous). The
`sessions` row holds the desk and network worlds, the acting member per world, and
the epoch, so act-as is a row update and never a new cookie. (ARCHITECTURE.md §11.1,
§11.3.)

`current_actor` resolves (session, world, member) for every request: the world is the
session's desk world unless the request says `?world=network`. A 401 always means
"call `POST /api/session/start`": no cookie, an unknown session, or a session whose
world was wiped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harmony.api.deps import ServicesDep, WorldServiceDep
from harmony.config import Settings
from harmony.db import repo
from harmony.db.models import Member, VisitorSession, World
from harmony.db.models.choices import MemberRole
from harmony.db.uow import unit_of_work
from harmony.events.bus import SESSION_CHANNEL
from harmony.events.schemas import EventScope, SessionMemberChanged
from harmony.worlds.service import WorldLens, member_column, world_column

SESSION_COOKIE = "harmony_session"
SESSION_COOKIE_MAX_AGE_S = 30 * 24 * 3600
_SALT = "harmony.session"
_START = "POST /api/session/start"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who a request acts as: resolved from the cookie, the session row and `?world=`.

    `world_id` is the session's desk world unless the request asked for
    `?world=network`; `member_id` is the session's acting member in that world.
    """

    session_id: UUID
    epoch: int
    lens: WorldLens
    world_id: UUID
    member_id: UUID
    org_id: UUID
    role: MemberRole


# --- The cookie -----------------------------------------------------------------------


def _serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret, salt=_SALT)


def sign_session_id(session_id: UUID, secret: str) -> str:
    return _serializer(secret).dumps(str(session_id))


def read_session_id(token: str, secret: str) -> UUID | None:
    """The session ID in a cookie value, or None if it is unsigned, tampered or malformed."""
    try:
        raw: object = _serializer(secret).loads(token)
    except BadSignature:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def set_session_cookie(response: Response, session_id: UUID, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_session_id(session_id, settings.session_secret_value()),
        max_age=SESSION_COOKIE_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "local",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def session_id_from_request(request: Request, settings: Settings) -> UUID | None:
    token = request.cookies.get(SESSION_COOKIE)
    if token is None:
        return None
    return read_session_id(token, settings.session_secret_value())


# --- Resolving the actor --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Resolved:
    actor: Actor
    build_sha: str


async def _resolve(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: UUID, lens: WorldLens
) -> _Resolved | None:
    world_id, member_id = world_column(lens), member_column(lens)
    statement = (
        select(
            VisitorSession.epoch, World.id, World.build_sha, Member.id, Member.org_id, Member.role
        )
        .select_from(VisitorSession)
        .join(World, and_(World.id == world_id, World.status == "live"))
        .join(Member, and_(Member.world_id == world_id, Member.id == member_id))
        .where(VisitorSession.id == session_id)
    )
    async with sessionmaker() as session:
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None
    epoch, world, build_sha, member, org, role = row
    actor = Actor(
        session_id=session_id,
        epoch=epoch,
        lens=lens,
        world_id=world,
        member_id=member,
        org_id=org,
        role=role,
    )
    return _Resolved(actor, build_sha)


async def resolve_actor(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: UUID, lens: WorldLens
) -> Actor | None:
    """The session's acting member in its live `lens` world; None if there is none."""
    resolved = await _resolve(sessionmaker, session_id, lens)
    return resolved.actor if resolved is not None else None


async def current_actor(
    request: Request,
    services: ServicesDep,
    worlds: WorldServiceDep,
    world: WorldLens = "desk",
) -> Actor:
    """FastAPI dependency: the acting identity for this request.

    401 without a valid session cookie, or when the session has no live world for
    `world` (the `?world=` query parameter); the client then calls
    `POST /api/session/start`. A world created under another build is forked first
    (`ensure_current_build`), so a deploy never serves an old build's world.
    """
    session_id = session_id_from_request(request, services.settings)
    if session_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"no session; {_START}")
    resolved = await _resolve(services.sessionmaker, session_id, world)
    if resolved is not None and resolved.build_sha != services.settings.world_build:
        await worlds.ensure_current_build(session_id, world)
        resolved = await _resolve(services.sessionmaker, session_id, world)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"no {world} world; {_START}")
    return resolved.actor


ActorDep = Annotated[Actor, Depends(current_actor)]


# --- Act-as ---------------------------------------------------------------------------


class MemberNotInWorld(LookupError):
    """Act-as named a member who isn't in the session's world (403)."""


class SessionMoved(RuntimeError):
    """The session switched worlds while the request ran (409)."""


async def act_as(
    sessionmaker: async_sessionmaker[AsyncSession], actor: Actor, member_id: UUID
) -> int:
    """Switch the session's acting member in `actor`'s world; returns the new epoch.

    The member must be in that world. Bumps the epoch, emits `session.member_changed`
    (session scope) and pokes the `session` channel, all in one transaction, so the
    session's open streams close and the client reopens them as the new member (§8).
    Raises `MemberNotInWorld`, `SessionMoved`, or `harmony.errors.WorldRetired`.
    """
    async with unit_of_work(sessionmaker, actor.world_id) as uow:
        session = uow.session
        # Checked here first so a stranger is refused with 403; the composite foreign
        # key (world, member) would refuse it at commit anyway.
        if await repo.get(session, Member, actor.world_id, member_id) is None:
            raise MemberNotInWorld(f"member {member_id} is not in world {actor.world_id}")
        visitor = await session.get(
            VisitorSession, actor.session_id, with_for_update=True, populate_existing=True
        )
        if visitor is None or _world_of(visitor, actor.lens) != actor.world_id:
            raise SessionMoved(f"session {actor.session_id} left world {actor.world_id}")
        if actor.lens == "desk":
            visitor.desk_member_id = member_id
        else:
            visitor.network_member_id = member_id
        visitor.epoch += 1
        visitor.last_seen_at = datetime.now(UTC)
        uow.add_event(
            SessionMemberChanged(
                session_id=actor.session_id, member_id=member_id, epoch=visitor.epoch
            ),
            EventScope.session(actor.session_id),
        )
        await session.execute(select(func.pg_notify(SESSION_CHANNEL, str(actor.session_id))))
        await uow.commit()
        return visitor.epoch


def _world_of(visitor: VisitorSession, lens: WorldLens) -> UUID | None:
    return visitor.desk_world_id if lens == "desk" else visitor.network_world_id
