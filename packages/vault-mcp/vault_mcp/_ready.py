"""Has Obsidian Sync caught up? Gates tool calls after an on-demand start, and the
idle shutdown before a stop.

Obsidian has no public Sync API, so this combines two signals:
- the internal Sync plugin's status, read with the CLI's ``eval`` (undocumented;
  statuses named in ``VAULT_SYNC_BUSY`` count as busy, anything else as idle);
- a quiet period: no file in the vault changed (ctime, which Sync can't
  backdate the way it preserves mtimes) for ``QUIET_SECONDS``.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from vault_mcp import _cli
from vault_mcp.config import Settings

QUIET_SECONDS = 30
# Sync hasn't even connected this early after boot, so "idle" would be a lie.
MIN_UPTIME = 60
# Past this, serve anyway (the tools fall back to the files if the app is down).
MAX_WAIT = 600
# The Sync probe must stay quick: /readyz runs inside the Lambda's 29 s budget.
PROBE_TIMEOUT = 5.0

_SYNC_JS = (
    "(()=>{const p=app.internalPlugins.getPluginById('sync');"
    "if(!p||!p.enabled)return JSON.stringify('disabled');const i=p.instance;"
    "return JSON.stringify(i&&i.getStatus?i.getStatus():i&&i.syncStatus)})()"
)
_latched = {"ready": False}


def uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return float("inf")  # not Linux (local dev): treat as long up


def last_change(root: Path) -> float:
    """Newest ctime of anything in the vault (a delete changes its folder's ctime)."""
    newest = root.stat().st_ctime
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for name in [*dirnames, *filenames]:
            try:
                newest = max(newest, Path(dirpath, name).lstat().st_ctime)
            except OSError:
                pass
    return newest


def sync_status(s: Settings) -> str | None:
    """The Sync plugin's status as text, or None when it can't be read."""
    try:
        out = _cli.run(s, "eval", _timeout=PROBE_TIMEOUT, code=_SYNC_JS).strip()
    except _cli.CliError:
        return None
    try:
        value: Any = json.loads(out)
    except ValueError:
        value = out
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def state(s: Settings) -> dict[str, Any]:
    app = _cli.app_running(s)
    status = sync_status(s) if app else None
    quiet = time.time() - last_change(s.vault_root)
    up = uptime()
    busy = status is not None and status.lower() in s.sync_busy
    settled = app and not busy and quiet >= QUIET_SECONDS and up >= MIN_UPTIME
    return {
        "app_running": app,
        "sync_status": status,
        "quiet_seconds": round(quiet),
        "uptime_seconds": round(up) if up != float("inf") else None,
        "settled": settled,
        "ready": settled or up >= MAX_WAIT,
    }


def check(
    s: Settings, st: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any] | None]:
    """``(ready, state)``, computing the state at most once (or using ``st``).

    Latched: once caught up after a start, later local writes don't re-gate, and
    the state isn't computed again (``None`` unless ``st`` was given).
    """
    if _latched["ready"]:
        return True, st
    st = st or state(s)
    _latched["ready"] = st["ready"]
    return st["ready"], st


def is_ready(s: Settings) -> bool:
    return check(s)[0]
