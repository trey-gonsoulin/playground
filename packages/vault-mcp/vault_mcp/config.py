"""Settings, read from the environment once per process (install.sh writes them)."""

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
    # Touched on every MCP request; the idle shutdown reads its mtime.
    activity_file: Path
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
    # On the on-demand box, refuse tool calls until Sync has caught up after a
    # start, so Claude never reads or edits a stale copy (see _ready.py).
    require_ready: bool
    # Sync statuses (from Obsidian's internal Sync plugin) that mean "busy".
    sync_busy: frozenset[str]
    # Run before bulk changes (a local git snapshot on the server); empty skips.
    snapshot_cmd: str
    # The snapshot repository (vault-snapshot's), for the git tools.
    git_dir: Path = Path("~/snapshots/vault.git")
    # Scripts in _Meta/scripts that run_script may run: (name, pinned sha256).
    scripts: tuple[tuple[str, str], ...] = ()
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
    busy = os.environ.get(
        "VAULT_SYNC_BUSY", "syncing,uploading,downloading,connecting,pending"
    )
    return Settings(
        vault_root=root,
        vault_name=os.environ.get("VAULT_NAME", root.name),
        readonly=os.environ.get("VAULT_MCP_READONLY", "").lower() in _TRUE,
        audit_log=Path(
            os.environ.get("VAULT_MCP_AUDIT_LOG", state / "vault-mcp" / "audit.log")
        ).expanduser(),
        activity_file=Path(
            os.environ.get(
                "VAULT_MCP_ACTIVITY_FILE", state / "vault-mcp" / "last_activity"
            )
        ).expanduser(),
        cli=os.environ.get("OBSIDIAN_CLI", "obsidian-cli"),
        cli_timeout=float(os.environ.get("OBSIDIAN_CLI_TIMEOUT", "20")),
        app_check=os.environ.get(
            "OBSIDIAN_APP_CHECK", "systemctl --user is-active --quiet obsidian.service"
        ),
        timezone=os.environ.get("VAULT_TZ", "America/Chicago"),
        require_ready=os.environ.get("VAULT_REQUIRE_READY", "").lower() in _TRUE,
        sync_busy=frozenset(x.strip().lower() for x in busy.split(",") if x.strip()),
        snapshot_cmd=os.environ.get("VAULT_SNAPSHOT_CMD", ""),
        git_dir=Path(os.environ.get("VAULT_GIT_DIR", "~/snapshots/vault.git"))
        .expanduser()
        .resolve(),
        scripts=_scripts(os.environ.get("VAULT_SCRIPTS", "")),
    )


def _scripts(spec: str) -> tuple[tuple[str, str], ...]:
    """``"a.py=<sha256>,b.py=<sha256>"`` -> pairs; malformed entries are dropped."""
    out = []
    for item in spec.split(","):
        name, _, digest = item.strip().partition("=")
        if name.strip() and len(digest.strip()) == 64:
            out.append((name.strip(), digest.strip().lower()))
    return tuple(out)
