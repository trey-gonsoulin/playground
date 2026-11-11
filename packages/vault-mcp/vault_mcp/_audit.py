"""Append-only JSON-lines log of every write, kept outside the vault."""

import json
from datetime import UTC, datetime
from typing import Any

from vault_mcp.config import Settings


def record(
    settings: Settings, tool: str, paths: list[str], summary: str, **extra: Any
) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": tool,
        "paths": paths,
        "summary": summary,
        **extra,
    }
    settings.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with settings.audit_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
