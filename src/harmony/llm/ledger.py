"""The development spend ledger: an append-only JSONL file shared with other projects.

`spend_ledger.jsonl` at the project root is the source of truth for cumulative dev
spend (§14). Other projects (counterbench) append to it too and sum `cost_usd` over
every line, so every harmony line carries a numeric `cost_usd` (0.0 for alerts and
acks). Harmony's own lines carry `project: "harmony"`; everything else is ignored.

Rules this module enforces:
- The file is never created, rewritten or truncated: writers open it with
  `O_APPEND` and no `O_CREAT`, under an exclusive `fcntl.flock`, and fsync each line.
- Readers take a shared lock and parse only complete lines. A malformed line that
  could be harmony's (it mentions the project) fails closed; other projects'
  malformed lines are counted and skipped.
- A ledger that shrank or was replaced since this process last read it fails closed:
  the spend total must never go down.

Open reservations (calls in flight, not yet settled) live in a sidecar file next to
the ledger, `.<ledger name>.<project>-inflight.json`, read and replaced only under the
ledger's exclusive lock, so every process that reserves against the cap sees every
other process's calls in flight. Entries of processes that have exited, or older than
`STALE_RESERVATION_S`, are dropped: such a process can no longer settle them.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from harmony.llm.errors import LedgerUnavailable

LedgerKind = Literal["call", "alert", "ack"]
AlertReason = Literal["crossed", "refused"]

_READ_CHUNK = 1 << 20
# No model call runs this long (the model activity's start-to-close is 600 s).
STALE_RESERVATION_S = 3600.0


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One line harmony writes. Every line carries the same core fields (null or zero
    where they don't apply); kind-specific extras are added only when set."""

    project: str
    kind: LedgerKind
    env: str
    cost_usd: Decimal = Decimal(0)
    world_id: str | None = None
    role: str | None = None
    model_requested: str | None = None
    model_served: str | None = None
    turn_id: str | None = None
    step_idx: int | None = None
    attempt: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    web_searches: int = 0
    ts: str = field(default_factory=utc_now_iso)
    # call: settled from a failed or cut-off call, and how the call ended.
    partial: bool | None = None
    stop_reason: str | None = None
    # alert
    threshold_usd: Decimal | None = None
    spent_usd: Decimal | None = None
    reason: AlertReason | None = None
    # ack
    acked_thresholds: tuple[Decimal, ...] | None = None

    def to_line(self) -> bytes:
        line: dict[str, object] = {
            "project": self.project,
            "kind": self.kind,
            "env": self.env,
            "world_id": self.world_id,
            "role": self.role,
            "model_requested": self.model_requested,
            "model_served": self.model_served,
            "turn_id": self.turn_id,
            "step_idx": self.step_idx,
            "attempt": self.attempt,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_write_5m_tokens": self.cache_write_5m_tokens,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
            "web_searches": self.web_searches,
            # A float, because other readers sum it with plain json.loads. Costs carry
            # at most six decimals, which a float's shortest repr preserves.
            "cost_usd": float(self.cost_usd),
            "ts": self.ts,
        }
        extras: dict[str, object | None] = {
            "partial": self.partial,
            "stop_reason": self.stop_reason,
            "threshold_usd": _float_or_none(self.threshold_usd),
            "spent_usd": _float_or_none(self.spent_usd),
            "reason": self.reason,
            "acked_thresholds": (
                [float(t) for t in self.acked_thresholds]
                if self.acked_thresholds is not None
                else None
            ),
        }
        line.update({key: value for key, value in extras.items() if value is not None})
        return (json.dumps(line, allow_nan=False) + "\n").encode("utf-8")


def _float_or_none(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class AlertRecord:
    threshold_usd: Decimal
    spent_usd: Decimal
    reason: str
    ts: str


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    project: str
    # Sum of `cost_usd` over every one of the project's lines, whatever its kind.
    spent_usd: Decimal
    calls: int
    alerts: tuple[AlertRecord, ...]
    acked: frozenset[Decimal]
    foreign_lines: int
    foreign_malformed: int

    @property
    def alerted(self) -> frozenset[Decimal]:
        return frozenset(alert.threshold_usd for alert in self.alerts)

    @property
    def unacked(self) -> tuple[AlertRecord, ...]:
        return tuple(alert for alert in self.alerts if alert.threshold_usd not in self.acked)


@dataclass(slots=True)
class _Scan:
    """Incremental parse state: lines up to `offset` are already counted."""

    identity: tuple[int, int] | None = None
    offset: int = 0
    spent: Decimal = Decimal(0)
    calls: int = 0
    alerts: list[AlertRecord] = field(default_factory=list[AlertRecord])
    acked: set[Decimal] = field(default_factory=set[Decimal])
    foreign: int = 0
    foreign_malformed: int = 0

    def summary(self, project: str) -> LedgerSummary:
        return LedgerSummary(
            project=project,
            spent_usd=self.spent,
            calls=self.calls,
            alerts=tuple(self.alerts),
            acked=frozenset(self.acked),
            foreign_lines=self.foreign,
            foreign_malformed=self.foreign_malformed,
        )


@dataclass(frozen=True, slots=True)
class OpenReservation:
    """A call in flight against the cap, by any process (the sidecar file)."""

    pid: int
    est_usd: Decimal
    since: float  # time.time() when reserved


class SpendLedger:
    """One ledger file. Not thread-safe: callers serialize use of an instance."""

    def __init__(self, path: Path, *, project: str = "harmony") -> None:
        self.path = path
        self.project = project
        self.in_flight_path = path.with_name(f".{path.name}.{project}-inflight.json")
        self._marker = f'"{project}"'.encode()
        self._scan = _Scan()

    def check_usable(self) -> None:
        """Raise `LedgerUnavailable` unless the ledger exists as a regular file that
        this process can read and append to. Never creates it."""
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            raise LedgerUnavailable(
                f"spend ledger {self.path} does not exist; it is never created "
                "automatically. Point SPEND_LEDGER_PATH at the project's spend_ledger.jsonl"
            ) from None
        except OSError as exc:
            raise LedgerUnavailable(f"spend ledger {self.path} is unreadable: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise LedgerUnavailable(f"spend ledger {self.path} is not a regular file")
        os.close(self._open(os.O_RDWR | os.O_APPEND))

    def read(self) -> LedgerSummary:
        """The current totals, read-only (a shared lock; never writes or creates)."""
        fd = self._open(os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._scan_new(fd)
        finally:
            os.close(fd)

    @contextmanager
    def locked(self) -> Generator[LockedLedger]:
        """Exclusive access for a read-check-append sequence."""
        fd = self._open(os.O_RDWR | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield LockedLedger(self, fd)
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def _open(self, flags: int) -> int:
        # No O_CREAT, ever: a missing ledger is an error, not something to recreate.
        try:
            return os.open(self.path, flags | os.O_CLOEXEC)
        except FileNotFoundError:
            raise LedgerUnavailable(f"spend ledger {self.path} does not exist") from None
        except OSError as exc:
            raise LedgerUnavailable(f"spend ledger {self.path} cannot be opened: {exc}") from exc

    def _scan_new(self, fd: int) -> LedgerSummary:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        scan = self._scan
        if scan.identity is not None and scan.identity != identity:
            raise LedgerUnavailable(f"spend ledger {self.path} was replaced; it is append-only")
        if info.st_size < scan.offset:
            raise LedgerUnavailable(f"spend ledger {self.path} shrank; it is append-only")
        scan.identity = identity
        data = _pread_all(fd, scan.offset, info.st_size - scan.offset)
        end = data.rfind(b"\n") + 1  # a line still being written is left for next time
        position = scan.offset
        for raw in data[:end].splitlines(keepends=True):
            self._ingest(raw, position)
            position += len(raw)
        scan.offset += end
        return scan.summary(self.project)

    def _ingest(self, raw: bytes, position: int) -> None:
        scan = self._scan
        if not raw.strip():
            return
        try:
            parsed: object = json.loads(raw, parse_float=Decimal)
        except ValueError:
            if self._marker in raw:
                raise LedgerUnavailable(
                    f"malformed {self.project} line at byte {position} of {self.path}"
                ) from None
            scan.foreign_malformed += 1
            return
        if not isinstance(parsed, dict):
            scan.foreign += 1
            return
        line = cast(dict[str, object], parsed)  # JSON object keys are always strings
        if line.get("project") != self.project:
            scan.foreign += 1
            return
        cost = _money(line.get("cost_usd"))
        if cost is None or cost < 0:
            raise LedgerUnavailable(
                f"{self.project} line at byte {position} of {self.path} has no valid cost_usd"
            )
        scan.spent += cost
        kind = line.get("kind")
        if kind == "alert":
            threshold = _money(line.get("threshold_usd"))
            if threshold is not None:
                scan.alerts.append(
                    AlertRecord(
                        threshold_usd=threshold,
                        spent_usd=_money(line.get("spent_usd")) or Decimal(0),
                        reason=str(line.get("reason") or "crossed"),
                        ts=str(line.get("ts") or ""),
                    )
                )
        elif kind == "ack":
            acked = line.get("acked_thresholds")
            if isinstance(acked, list):
                scan.acked.update(
                    money
                    for item in acked  # pyright: ignore[reportUnknownVariableType]
                    if (money := _money(item)) is not None  # pyright: ignore[reportUnknownArgumentType]
                )
        else:
            scan.calls += 1


class LockedLedger:
    """The ledger under an exclusive lock (from `SpendLedger.locked`)."""

    def __init__(self, ledger: SpendLedger, fd: int) -> None:
        self._ledger = ledger
        self._fd = fd

    def summary(self) -> LedgerSummary:
        return self._ledger._scan_new(self._fd)  # pyright: ignore[reportPrivateUsage]

    def append(self, entries: Sequence[LedgerEntry]) -> None:
        """Append whole lines and fsync. A foreign line left without its newline (a
        writer that died mid-line) is terminated first, so ours stays parseable."""
        if not entries:
            return
        data = b"".join(entry.to_line() for entry in entries)
        size = os.fstat(self._fd).st_size
        if size > 0 and os.pread(self._fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        os.fsync(self._fd)

    def open_reservations(self) -> dict[str, OpenReservation]:
        """Every process's open reservations, minus those no process can settle any more."""
        path = self._ledger.in_flight_path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise LedgerUnavailable(f"open reservations {path} are unreadable: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            parsed: object = json.loads(raw, parse_float=Decimal)
            entries = cast(dict[str, dict[str, object]], parsed)
            reservations = {
                key: OpenReservation(
                    pid=int(cast(int, value["pid"])),
                    est_usd=Decimal(str(value["est_usd"])),
                    since=float(cast(float, value["since"])),
                )
                for key, value in entries.items()
            }
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            # Fail closed: an unreadable file could be hiding calls in flight.
            raise LedgerUnavailable(
                f"open reservations {path} are malformed; delete the file once no harmony "
                "process is making live calls"
            ) from None
        now = time.time()
        return {
            key: entry
            for key, entry in reservations.items()
            if now - entry.since < STALE_RESERVATION_S and _process_alive(entry.pid)
        }

    def save_open_reservations(self, reservations: Mapping[str, OpenReservation]) -> None:
        """Replace the sidecar file atomically (a reader sees the old or the new set)."""
        path = self._ledger.in_flight_path
        body = json.dumps(
            {
                key: {"pid": entry.pid, "est_usd": str(entry.est_usd), "since": entry.since}
                for key, entry in reservations.items()
            },
            sort_keys=True,
        ).encode()
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, path)
        except OSError as exc:
            raise LedgerUnavailable(f"open reservations {path} cannot be written: {exc}") from exc

    def add_open_reservation(self, key: str, reservation: OpenReservation) -> None:
        reservations = self.open_reservations()
        reservations[key] = reservation
        self.save_open_reservations(reservations)

    def remove_open_reservation(self, key: str) -> None:
        reservations = self.open_reservations()
        reservations.pop(key, None)
        self.save_open_reservations(reservations)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    return True


def _pread_all(fd: int, offset: int, length: int) -> bytes:
    chunks: list[bytes] = []
    while length > 0:
        chunk = os.pread(fd, min(length, _READ_CHUNK), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        length -= len(chunk)
    return b"".join(chunks)


def _money(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


__all__ = [
    "STALE_RESERVATION_S",
    "AlertReason",
    "AlertRecord",
    "LedgerEntry",
    "LedgerKind",
    "LedgerSummary",
    "LockedLedger",
    "OpenReservation",
    "SpendLedger",
    "utc_now_iso",
]
