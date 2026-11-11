"""The official Obsidian CLI (``obsidian-cli``, Obsidian 1.12.7+), driven by subprocess.

The CLI is a remote control for the running app, so it is used for what only the
app does well (Obsidian's search, link-rewriting moves, trash, daily notes) and
the result is then checked on disk. Its contract is loose: the exit code is
always 0, errors are printed to stdout as ``Error: ...``, and the ``format=json``
shapes are undocumented. So callers parse defensively and fall back to the
filesystem when the output doesn't look right.
"""

import json
import os
import shlex
import subprocess
import time
from typing import Any

from vault_mcp.config import Settings

_APP_CHECK_TTL = 5.0
_app_state: dict[str, tuple[float, bool]] = {}


class CliError(RuntimeError):
    """The CLI ran but reported an error, or its output made no sense."""


class CliUnavailable(CliError):
    """The CLI can't be used right now (app down, not installed, timed out)."""


def app_running(settings: Settings) -> bool:
    """Whether the Obsidian app is up, cached for a few seconds."""
    if not settings.app_check:
        return True
    now = time.monotonic()
    cached = _app_state.get(settings.app_check)
    if cached and now - cached[0] < _APP_CHECK_TTL:
        return cached[1]
    try:
        ok = (
            subprocess.run(  # noqa: S603 -- operator-configured command
                shlex.split(settings.app_check),
                capture_output=True,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _app_state[settings.app_check] = (now, ok)
    return ok


def run(
    settings: Settings,
    command: str,
    *flags: str,
    _timeout: float | None = None,
    **params: Any,
) -> str:
    """Run ``obsidian-cli vault=<name> <command> key=value... flags...``; return stdout.

    ``None`` params are left out. Raises ``CliUnavailable`` when the app is down
    (rather than letting the CLI launch a second, unsupervised app) and
    ``CliError`` when the CLI reports an error. ``_timeout`` overrides
    ``OBSIDIAN_CLI_TIMEOUT`` for quick probes.
    """
    timeout = settings.cli_timeout if _timeout is None else _timeout
    if not settings.cli:
        raise CliUnavailable("Obsidian CLI disabled (OBSIDIAN_CLI is empty)")
    if not app_running(settings):
        raise CliUnavailable("Obsidian app is not running")
    args = [*shlex.split(settings.cli), f"vault={settings.vault_name}", command]
    args += [f"{k}={v}" for k, v in params.items() if v is not None]
    args += flags
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")}
    try:
        proc = subprocess.run(  # noqa: S603 -- argv list, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliUnavailable(f"Obsidian CLI not found: {settings.cli}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliUnavailable(f"Obsidian CLI timed out after {timeout}s") from exc
    out = proc.stdout.strip()
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or out)[:300]
        raise CliUnavailable(f"Obsidian CLI exited {proc.returncode}: {detail}")
    if out.startswith("Error"):
        raise CliError(out.splitlines()[0][:300])
    if out.startswith("{"):
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise CliError(str(payload.get("error", "unknown CLI error"))[:300])
    return out


def run_json(settings: Settings, command: str, *flags: str, **params: Any) -> Any:
    out = run(settings, command, *flags, format="json", **params)
    try:
        return json.loads(out) if out else None
    except ValueError as exc:
        raise CliError(
            f"unexpected non-JSON output from `{command}`: {out[:120]!r}"
        ) from exc
