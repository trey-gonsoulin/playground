"""Write locks: one session reserves notes or folders for a batch of writes.

Several Claude sessions share the connector's one token, so a lock is
cooperative: ``begin_write`` records who holds which paths, until when and why,
and every write checks it (``Vault`` calls ``check`` before touching a file).
A write to a locked path must name the lock (the tools' ``lock`` argument, held
in ``CURRENT`` for the call); a write naming a lock must stay inside it. Locks
live in a JSON file next to the audit log, so they survive restarts, and an
expired lock simply stops counting.
"""

import contextlib
import json
import secrets
import time
from collections.abc import Generator
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from vault_mcp.config import Settings

# The lock named by the tool call in progress.
CURRENT: ContextVar[str | None] = ContextVar("vault_lock", default=None)

MAX_MINUTES = 240


class LockError(ValueError):
    """A write or lock request that conflicts with a lock."""


@dataclass
class Lock:
    id: str
    owner: str
    paths: list[str]  # notes/files
    folders: list[str]  # "" is the whole vault
    reason: str
    taken: float
    expires: float

    def covers(self, rel: str) -> bool:
        return rel in self.paths or any(
            f == "" or rel.startswith(f + "/") for f in self.folders
        )

    def overlaps(self, other: "Lock") -> bool:
        return (
            any(other.covers(p) for p in self.paths)
            or any(self.covers(p) for p in other.paths)
            or any(
                a == ""
                or b == ""
                or a == b
                or a.startswith(b + "/")
                or b.startswith(a + "/")
                for a in self.folders
                for b in other.folders
            )
        )

    def public(self) -> dict[str, object]:
        out = asdict(self)
        out["taken"] = _iso(self.taken)
        out["expires"] = _iso(self.expires)
        return out


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _file(s: Settings) -> Path:
    return s.audit_log.parent / "locks.json"


def live(s: Settings) -> list[Lock]:
    """Locks that haven't expired."""
    try:
        raw = json.loads(_file(s).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    now = time.time()
    return [lk for lk in (Lock(**x) for x in raw) if lk.expires > now]


def _save(s: Settings, locks: list[Lock]) -> None:
    path = _file(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(lk) for lk in locks], indent=1), "utf-8")
    tmp.replace(path)


def take(
    s: Settings,
    owner: str,
    paths: list[str],
    folders: list[str],
    reason: str,
    minutes: float,
) -> Lock:
    """A new lock, unless it overlaps one held by another owner."""
    now = time.time()
    minutes = max(1.0, min(float(minutes), MAX_MINUTES))
    new = Lock(
        secrets.token_hex(6), owner, paths, folders, reason, now, now + minutes * 60
    )
    locks = live(s)
    for lk in locks:
        if lk.owner != owner and lk.overlaps(new):
            raise LockError(f"overlaps {describe(lk)}")
    _save(s, [*locks, new])
    return new


def get(s: Settings, lock_id: str) -> Lock:
    for lk in live(s):
        if lk.id == lock_id:
            return lk
    raise LockError(f"no live lock {lock_id!r} (it expired or was released)")


def release(s: Settings, lock_id: str) -> None:
    _save(s, [lk for lk in live(s) if lk.id != lock_id])


def describe(lk: Lock) -> str:
    return f"lock {lk.id} held by {lk.owner!r} ({lk.reason}) until {_iso(lk.expires)}"


def check(s: Settings, rel: str) -> None:
    """Refuse a write to ``rel`` that a lock forbids."""
    cur = CURRENT.get()
    locks = live(s)
    mine = next((lk for lk in locks if lk.id == cur), None)
    if cur and mine is None:
        raise LockError(
            f"lock {cur!r} has expired or was released; take a new one with begin_write"
        )
    for lk in locks:
        if lk.id != cur and lk.covers(rel):
            raise LockError(
                f"{rel} is reserved by {describe(lk)}; wait, or ask that session "
                "to end_write"
            )
    if mine and not mine.covers(rel):
        raise LockError(f"{rel} is outside lock {mine.id}; widen it with a new lock")


@contextlib.contextmanager
def using(lock: str | None) -> Generator[None]:
    token = CURRENT.set(lock or None)
    try:
        yield
    finally:
        CURRENT.reset(token)
