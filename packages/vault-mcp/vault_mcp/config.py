"""Settings, read from the environment once per process (see ``deploy/vault-mcp.env``)."""

import os
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    vault_root: Path
    vault_name: str
    readonly: bool
    audit_log: Path
    # Command for the official Obsidian CLI; on the server, the DISPLAY=:99 wrapper.
    # Empty disables the CLI (filesystem only).
    cli: str
    cli_timeout: float
    # Exits 0 when the Obsidian app is running. Checked before every CLI call,
    # because the CLI would otherwise launch a second app outside systemd.
    # Empty skips the check.
    app_check: str
    # Dates ("today", ``created``) are in this zone, not the server's UTC.
    timezone: str
    # Writes touching more files than this need dry_run=False (brief §6.3).
    bulk_threshold: int = 10

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self) -> date:
        return datetime.now(self.tz).date()


@cache
def get_settings() -> Settings:
    root = Path(os.environ.get("VAULT_ROOT", "~/vaults/Trey")).expanduser().resolve()
    state = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return Settings(
        vault_root=root,
        vault_name=os.environ.get("VAULT_NAME", root.name),
        readonly=os.environ.get("VAULT_MCP_READONLY", "").lower() in _TRUE,
        audit_log=Path(
            os.environ.get("VAULT_MCP_AUDIT_LOG", state / "vault-mcp" / "audit.log")
        ).expanduser(),
        cli=os.environ.get("OBSIDIAN_CLI", "obsidian-cli"),
        cli_timeout=float(os.environ.get("OBSIDIAN_CLI_TIMEOUT", "20")),
        app_check=os.environ.get(
            "OBSIDIAN_APP_CHECK", "systemctl --user is-active --quiet obsidian.service"
        ),
        timezone=os.environ.get("VAULT_TZ", "America/Chicago"),
    )
