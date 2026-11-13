"""Git on the snapshot repository: history, diffs and commits of the vault.

The repository is vault-snapshot's: its git dir is outside the vault (so
Obsidian Sync never sees it) and its work tree is the vault. vault-snapshot
commits everything before bulk edits and idle stops; ``commit`` makes a
deliberate commit of chosen paths. Pathspecs are literal (no globs).
"""

import os
import subprocess

from vault_mcp.config import Settings

IDENTITY = ("-c", "user.name=vault-mcp", "-c", "user.email=vault-mcp@localhost")


class GitError(RuntimeError):
    """git failed, or the snapshot repository doesn't exist."""


def run(s: Settings, *args: str, timeout: float = 20.0) -> str:
    return run_bytes(s, *args, timeout=timeout).decode("utf-8", "replace")


def run_bytes(s: Settings, *args: str, timeout: float = 20.0) -> bytes:
    if not (s.git_dir / "HEAD").is_file():
        raise GitError(
            f"no snapshot repository at {s.git_dir}; vault-snapshot creates it "
            "on the first bulk edit or idle stop"
        )
    env = {
        **os.environ,
        "GIT_DIR": str(s.git_dir),
        "GIT_WORK_TREE": str(s.vault_root),
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        proc = subprocess.run(  # noqa: S603 -- argv list, no shell
            ["git", "-c", "core.quotePath=false", *IDENTITY, *args],  # noqa: S607
            capture_output=True,
            timeout=timeout,
            env=env,
            cwd=s.vault_root,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {args[0]}: {err[:400]}")
    return proc.stdout


def status(s: Settings) -> list[dict[str, str]]:
    """Changed paths since the last commit: ``{"status", "path"}`` (``??`` = new)."""
    out = run(s, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        item = {"status": code.strip() or code, "path": path}
        if code[0] in "RC":  # the next field is the old path
            item["from"] = parts[i]
            i += 1
        items.append(item)
    return items


def head(s: Settings) -> str | None:
    try:
        return run(s, "rev-parse", "--short", "HEAD").strip()
    except GitError:
        return None


def commit(
    s: Settings, message: str, paths: list[str] | None = None
) -> dict[str, object]:
    """Commit ``paths`` (default: everything) as they are now; ``commit`` is None if unchanged."""
    spec = ["--", *paths] if paths else ["--", "."]
    run(s, "add", "-A", *spec)
    staged = run(s, "diff", "--cached", "--no-renames", "--name-status", *spec).strip()
    if not staged:
        return {"commit": None, "note": "nothing to commit"}
    files = [line.split("\t") for line in staged.splitlines()]
    # Name the staged files: a lock may cover a folder or note with no changes,
    # which git commit would reject as an unknown pathspec.
    run(s, "commit", "-q", "-m", message, "--", *(f[-1] for f in files))
    return {
        "commit": head(s),
        "files": [{"status": f[0], "path": f[-1]} for f in files][:200],
        "total_files": len(files),
    }
