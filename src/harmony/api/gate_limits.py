"""The passcode gate's in-process limits (LIVE_DEMO_PLAN 3.2, 3.3): unlock attempts
per client key and per wider network, the global pacing of wrong passcodes, and Resets
per session.

They hold state for one web process (Render runs one instance) and are created per
app with its `Gate` (`api/gate.py`).

Owner: L1.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from harmony.worlds.service import TooManyWorlds

log = logging.getLogger(__name__)

# Each wrong passcode waits for one such slot behind a global lock, so the server
# confirms about 3 wrong guesses a second however many clients send them.
FAILURE_SLOT_S: Final = 0.3
# At most this many wrong passcodes wait for a slot; one beyond that is answered at once.
# A flood from many keys then holds a bounded number of requests, and a mistyped
# passcode is answered within this many slots. Each key's attempt limit, and its
# network's, still bound the guesses it gets answered.
MAX_WAITING: Final = 20

# At most this many clients, networks (or sessions) are tracked. At the cap, idle
# entries are dropped oldest first, so an attempt never walks the whole table.
MAX_TRACKED: Final = 20_000


def _make_room[K, V](table: OrderedDict[K, V], limit: int, idle: Callable[[V], bool]) -> bool:
    """Drop idle entries from the front of `table` (least recently touched first) until
    one more fits, stopping at the first live one so each call does little work.
    Whether there is room."""
    while len(table) >= limit:
        oldest = next(iter(table))
        if not idle(table[oldest]):
            return False
        del table[oldest]
    return True


@dataclass(slots=True)
class _Client:
    attempts: deque[float] = field(default_factory=deque[float])
    blocked_until: float = 0.0
    lockouts: int = 0
    in_flight: bool = False


@dataclass(slots=True)
class _Network:
    """Attempts from one wider network in the window that began at `started`."""

    started: float
    attempts: int = 0


class UnlockLimiter:
    """Unlock attempts per known client key, and per the wider network it is in.

    A client key gets `attempts` per `window_s`, then a block that doubles with each
    lockout up to `max_block_s`, and one attempt in flight. A client whose last block
    ended `max_block_s` or more ago starts over at the shortest block. A network (an
    IPv4 /24, an IPv6 /48: `gate.client_network`) gets `network_attempts` per window
    across all its keys, so the 65,536 /64 keys one routed /48 holds don't each bring
    a fresh allowance.

    Attempts are counted, not failures, so parallel guesses are refused before any is
    compared. A success forgets the client and gives its network the attempt back.

    While `max_tracked` clients are all live, a new key goes untracked as a client and
    meets only its network's limit: throttling it instead would hand whoever filled the
    table a lockout of every new visitor. While `max_tracked` networks are all live, a
    new network is refused until one goes idle: getting there takes that many networks,
    and an untracked one would have no bound at all.
    """

    def __init__(
        self,
        *,
        attempts: int = 5,
        network_attempts: int = 50,
        window_s: float = 15 * 60,
        max_block_s: float = 60 * 60,
        max_tracked: int = MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._attempts = attempts
        self._network_attempts = network_attempts
        self._window_s = window_s
        self._max_block_s = max_block_s
        self._max_tracked = max_tracked
        self._clock = clock
        # Least recently touched first, so idle entries are dropped from the front.
        self._clients: OrderedDict[str, _Client] = OrderedDict()
        self._networks: OrderedDict[str, _Network] = OrderedDict()
        self._full_logged_at: dict[str, float] = {}

    def admit(self, key: str, network: str) -> int | None:
        """Count an attempt from `key`, in `network`: None to go ahead (settle it
        after), or the seconds to wait. Synchronous, so nothing runs between the check
        and the count."""
        now = self._clock()
        client = self._client(key, now)
        if client is not None:
            wait_s = self._client_wait(client, now)
            if wait_s is not None:
                return wait_s
        tally = self._network(network, now)
        if tally is None:
            # Room comes when the least recently touched network's window ends.
            oldest = next(iter(self._networks.values()))
            return max(1, math.ceil(oldest.started + self._window_s - now))
        if tally.attempts >= self._network_attempts:
            return max(1, math.ceil(tally.started + self._window_s - now))
        tally.attempts += 1
        if client is not None:
            client.attempts.append(now)
            client.in_flight = True
        return None

    def settle(self, key: str, network: str, *, success: bool) -> None:
        """The attempt from `key` was answered, or compared and wrong: the key may try
        again (a wrong one settles before its pacing wait, so a typo's correction never
        queues behind it)."""
        if success:
            self._clients.pop(key, None)
            if (tally := self._networks.get(network)) is not None:
                tally.attempts = max(0, tally.attempts - 1)
        elif (client := self._clients.get(key)) is not None:
            client.in_flight = False

    def _client(self, key: str, now: float) -> _Client | None:
        """`key`'s entry, made if there is room; None while every tracked one is live."""
        client = self._clients.get(key)
        if client is not None:
            self._clients.move_to_end(key)
        elif _make_room(self._clients, self._max_tracked, lambda c: self._idle(c, now)):
            client = self._clients[key] = _Client()
        else:
            self._log_full("clients", len(self._clients), now)
        return client

    def _client_wait(self, client: _Client, now: float) -> int | None:
        """Seconds `client` must wait (starting a block when its allowance is spent)."""
        if client.in_flight:
            return 1
        if now < client.blocked_until:
            return math.ceil(client.blocked_until - now)
        while client.attempts and client.attempts[0] <= now - self._window_s:
            client.attempts.popleft()
        if len(client.attempts) >= self._attempts:
            if now - client.blocked_until >= self._max_block_s:
                client.lockouts = 0
            client.lockouts += 1
            block = min(self._window_s * 2 ** (client.lockouts - 1), self._max_block_s)
            client.blocked_until = now + block
            client.attempts.clear()
            return math.ceil(block)
        return None

    def _network(self, network: str, now: float) -> _Network | None:
        """`network`'s tally for the current window; None while every tracked one is live."""
        tally = self._networks.get(network)
        if tally is not None:
            self._networks.move_to_end(network)
            if tally.started <= now - self._window_s:
                tally.started, tally.attempts = now, 0
        elif _make_room(
            self._networks, self._max_tracked, lambda t: t.started <= now - self._window_s
        ):
            tally = self._networks[network] = _Network(started=now)
        else:
            self._log_full("networks", len(self._networks), now)
        return tally

    def _idle(self, client: _Client, now: float) -> bool:
        return (
            not client.in_flight
            and client.blocked_until <= now
            and all(at <= now - self._window_s for at in client.attempts)
        )

    def _log_full(self, table: str, tracked: int, now: float) -> None:
        logged_at = self._full_logged_at.get(table)
        if logged_at is None or now - logged_at >= self._window_s:
            self._full_logged_at[table] = now
            log.warning("gate_%s_full tracked=%d", table, tracked)


class FailurePacer:
    """The global soft measure: each wrong passcode waits for a `FAILURE_SLOT_S` slot
    behind one lock, unless `MAX_WAITING` already do, and a spike of failures is logged
    once per window. There is no global lockout: it would shut out every visitor and
    the operator.

    Failures are counted per fixed window, so a flood costs a counter, not a timestamp
    per failure."""

    def __init__(
        self,
        *,
        spike: int = 100,
        window_s: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spike = spike
        self._window_s = window_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._window_started: float | None = None
        self._failures = 0
        self._spike_logged = False
        self._waiting = 0

    @property
    def waiting(self) -> int:
        """Failures waiting for their slot now."""
        return self._waiting

    async def wait(self) -> None:
        now = self._clock()
        if self._window_started is None or self._window_started <= now - self._window_s:
            self._window_started, self._failures, self._spike_logged = now, 0, False
        self._failures += 1
        if self._failures > self._spike and not self._spike_logged:
            self._spike_logged = True
            log.warning(
                "gate_failures_spike failures=%d window_s=%d", self._failures, self._window_s
            )
        if self._waiting >= MAX_WAITING:
            return
        self._waiting += 1
        try:
            async with self._lock:
                await asyncio.sleep(FAILURE_SLOT_S)
        finally:
            self._waiting -= 1


class WorldLimits:
    """World creation while the gate is on (3.3), implementing
    `worlds.service.CreationLimits`.

    New sessions are not limited. Resets and wiped-world re-creates are counted per
    session (`recreates` an hour), never per client key, so a shared address can't
    throttle anyone else's Resets.
    """

    def __init__(
        self,
        *,
        recreates: int = 20,
        recreate_window_s: float = 60 * 60,
        max_tracked: int = MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._recreates = recreates
        self._recreate_window_s = recreate_window_s
        self._max_tracked = max_tracked
        self._clock = clock
        # Least recently re-created first, so stale sessions are dropped from the front.
        self._by_session: OrderedDict[UUID, deque[float]] = OrderedDict()

    def recreate(self, session_id: UUID) -> None:
        now = self._clock()
        recent = self._by_session.get(session_id)
        if recent is not None:
            self._by_session.move_to_end(session_id)
        elif _make_room(
            self._by_session,
            self._max_tracked,
            lambda times: not times or times[-1] <= now - self._recreate_window_s,
        ):
            recent = self._by_session[session_id] = deque()
        else:
            return
        while recent and recent[0] <= now - self._recreate_window_s:
            recent.popleft()
        if len(recent) >= self._recreates:
            raise TooManyWorlds(max(1, math.ceil(recent[0] + self._recreate_window_s - now)))
        recent.append(now)


__all__ = [
    "FAILURE_SLOT_S",
    "MAX_TRACKED",
    "MAX_WAITING",
    "FailurePacer",
    "UnlockLimiter",
    "WorldLimits",
]
