"""MCP tools for an Obsidian vault on an always-on server (brief §6.3).

The official Obsidian CLI is used for search, moves/renames, deletes, tasks and
creating today's daily note; everything else, and every CLI action whose app is
down, goes straight to the files. Each result names the ``backend`` it used.
Write tools follow ``_Meta/Vault Guide.md``: new notes land in ``_Inbox`` with
``type``/``status``/``created``, renames keep the old title in ``aliases``,
nothing is deleted permanently, and every write is audit-logged.
"""

import base64
import difflib
import functools
import hashlib
import inspect
import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from vault_mcp import _archive, _audit, _cli, _git, _locks, _notes, _ready
from vault_mcp._vault import Note, Vault, VaultError
from vault_mcp.config import Settings, get_settings

CLI = "obsidian-cli"
FS = "filesystem"
_MAX_SNIPPET = 240


@cache
def _vault() -> Vault:
    return Vault(get_settings())


def _ctx() -> tuple[Settings, Vault]:
    v = _vault()
    if not v.root.is_dir():
        raise ToolError(f"vault not found at {v.root}")
    return v.settings, v


def _find(v: Vault, note: str) -> Note:
    try:
        return v.find(note)
    except VaultError as exc:
        raise ToolError(str(exc)) from exc


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a write tool with its ``lock`` argument as the lock in force (see _locks)."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def tool(*args: Any, **kwargs: Any) -> Any:
        lock = sig.bind_partial(*args, **kwargs).arguments.get("lock")
        with _locks.using(lock):
            return fn(*args, **kwargs)

    return tool


def _lock_check(s: Settings, *paths: str) -> None:
    """For writes the CLI makes (moves, deletes), which bypass ``Vault``'s check."""
    for path in paths:
        try:
            _locks.check(s, path)
        except _locks.LockError as exc:
            raise ToolError(str(exc)) from exc


def _receipt(rel: str, data: str | bytes) -> dict[str, Any]:
    """What a write returns, so a bulk job can verify without reading back."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return {"path": rel, "version": Vault.version(raw), "bytes": len(raw)}


_CURRENT_SHOWN = 20_000


def _check_version(rel: str, text: str, if_version: str | None) -> None:
    """Refuse an edit based on an older version, returning the current text."""
    if not if_version or Vault.version(text) == if_version:
        return
    more = (
        f"\n[... {len(text) - _CURRENT_SHOWN} more characters; read_note for the rest]"
        if len(text) > _CURRENT_SHOWN
        else ""
    )
    raise ToolError(
        f"{rel} changed since version {if_version[:12]}; nothing was written. "
        f"Current version: {Vault.version(text)}. Current text:\n"
        f"{text[:_CURRENT_SHOWN]}{more}"
    )


_COMPATIBLE = {frozenset({"date", "datetime"})}


def _type_errors(v: Vault, props: dict[str, Any], path: str | None = None) -> list[str]:
    """Properties given a type that no other note uses for them.

    Only properties with one type across at least two other notes are checked,
    so a property in flux (mixed types) is left alone.
    """
    want = {k: kd for k, val in props.items() if (kd := _notes.kind(val))}
    if not want:
        return []
    seen: dict[str, Counter[str]] = {}
    for n in v.notes():
        if n.path == path:
            continue
        for k in want:
            if kd := n.kinds.get(k):
                seen.setdefault(k, Counter())[kd] += 1
    errors = []
    for k, kd in want.items():
        counts = seen.get(k)
        if not counts or len(counts) != 1:
            continue
        [(other, count)] = counts.items()
        if count < 2 or other == kd or frozenset({kd, other}) in _COMPATIBLE:
            continue
        hint = {
            ("text", "number"): "pass it as a number, not a string",
            ("number", "text"): 'pass it as a string ("10.10") to keep it text',
            ("text", "list"): "pass a list",
            ("text", "boolean"): "pass true or false, not a string",
        }.get((kd, other), f"give a {other}")
        errors.append(
            f"{k}: {props[k]!r} is a {kd}, but it's a {other} in all {count} other "
            f"notes with it; {hint}"
        )
    return errors


def _check_types(v: Vault, props: dict[str, Any], path: str | None = None) -> None:
    if errors := _type_errors(v, props, path):
        raise ToolError("property types don't match the vault: " + "; ".join(errors))


def _wait_for(pred: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _today_str(s: Settings) -> str:
    return s.today().isoformat()


def _check_vocab(props: dict[str, Any]) -> list[str]:
    """Reject unknown type/status/source; return warnings for off-list tags."""
    for key, allowed in (
        ("type", _notes.TYPES),
        ("status", _notes.STATUSES),
        ("source", _notes.SOURCES),
    ):
        if props.get(key) is not None and props[key] not in allowed:
            raise ToolError(
                f"{key} must be one of {', '.join(allowed)}; got {props[key]!r}"
            )
    extra = [t for t in _notes.as_list(props.get("tags")) if t not in _notes.THEME_TAGS]
    if extra:
        return [
            f"tags {extra} aren't in the Vault Guide's theme list "
            f"({', '.join(_notes.THEME_TAGS)}); prefer folders or type"
        ]
    return []


# --- read tools -----------------------------------------------------------------


def _parse_cli_paths(out: str) -> list[str]:
    """Paths from ``search`` output: a JSON list of paths or objects, or text lines."""
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    paths: list[str] = []
    if isinstance(data, list):
        for item in data:
            p = item if isinstance(item, str) else None
            if isinstance(item, dict):
                p = item.get("path") or item.get("file")
            if isinstance(p, str):
                paths.append(p)
    elif data is None:
        paths = [ln.split(":", 1)[0].strip() for ln in out.splitlines()]
    else:
        raise _cli.CliError(f"unexpected search output: {out[:120]!r}")
    return list(dict.fromkeys(p for p in paths if p.endswith(".md")))


def _terms(query: str) -> list[str]:
    try:
        parts = shlex.split(query)
    except ValueError:
        parts = query.split()
    return [p.lower() for p in parts if p.strip()]


def _snippets(text: str, terms: list[str], max_snips: int = 3) -> list[dict[str, Any]]:
    """The lines matching the most terms (whole phrase first), with a line of context."""
    lines = text.split("\n")
    plain = [t for t in terms if ":" not in t] or terms
    phrase = " ".join(plain)
    scored = []
    for i, line in enumerate(lines):
        low = line.lower()
        hits = sum(t in low for t in plain)
        if hits:
            scored.append((-(hits + (len(plain) if phrase in low else 0)), i))
    best = [i for _, i in sorted(scored)[:max_snips]]  # best first
    out = []
    for i in best:
        ctx = [ln.strip() for ln in lines[max(0, i - 1) : i + 2] if ln.strip()]
        out.append({"line": i + 1, "text": " ⏎ ".join(ctx)[:_MAX_SNIPPET]})
    return out


def _fs_search(v: Vault, terms: list[str], folder: str | None) -> list[Note]:
    scored = []
    for n in v.notes():
        if folder and not n.path.startswith(folder.rstrip("/") + "/"):
            continue
        text = v.read_text(n.path).lower()
        score = 0
        for t in terms:
            if t.startswith("tag:"):
                hit = t[4:].lstrip("#") in n.tags
            elif t.startswith("path:"):
                hit = t[5:] in n.path.lower()
            else:
                hit = t in text or t in n.path.lower()
                # A title hit outweighs many body mentions (e.g. in a log).
                score += min(text.count(t), 10) + (25 if t in n.title.lower() else 0)
            if not hit:
                break
        else:
            scored.append((score, n))
    scored.sort(key=lambda x: (-x[0], x[1].path))
    return [n for _, n in scored]


def search(query: str, folder: str | None = None, limit: int = 15) -> dict[str, Any]:
    """Full-text search of the vault, with matching lines as context.

    Uses Obsidian's own search, so its operators work (``tag:#dnd``,
    ``path:TTRPG``, ``"exact phrase"``, ``-exclude``); if the app is down it
    searches the files (terms must all appear; ``tag:``/``path:`` supported).

    Args:
        query: Words or an Obsidian search query.
        folder: Only search under this vault folder, e.g. "Personal".
        limit: Maximum notes to return (1-50).
    """
    s, v = _ctx()
    limit = max(1, min(limit, 50))
    terms = _terms(query)
    if not terms:
        raise ToolError("query is empty")
    backend, note = CLI, None
    try:
        out = _cli.run(
            s, "search", query=query, path=folder, limit=limit, format="json"
        )
        paths = [p for p in _parse_cli_paths(out) if v.resolve(p).is_file()]
        if not paths:
            raise _cli.CliError("no results from Obsidian search")
        found = [v.find(p) for p in paths[:limit]]
    except (_cli.CliError, VaultError) as exc:
        backend, note = FS, f"searched the files directly ({exc})"
        found = _fs_search(v, terms, folder)[:limit]
    results = []
    for n in found:
        r = n.summary()
        text = v.read_text(n.path)
        r["matches"] = _snippets(text, terms)
        if not r["matches"]:  # matched on the title or path only
            r["preview"] = " ".join(_notes.split(text)[1].split())[:_MAX_SNIPPET]
        results.append(r)
    out_d: dict[str, Any] = {
        "backend": backend,
        "count": len(results),
        "results": results,
    }
    if note:
        out_d["note"] = note
    return out_d


def read_note(
    note: str, offset: int = 0, limit: int = 200, heading: str | None = None
) -> dict[str, Any]:
    """Read a note: body (paged by line), frontmatter, outgoing links and backlinks.

    Frontmatter values are typed as Obsidian reads them (numbers, booleans,
    lists; dates as text). ``version`` is the file's sha256: pass it as
    ``if_version`` to update_note/append_to_note so a write refuses if the
    note changed in between.

    Args:
        note: Vault path ("Health/Health.md"), title ("Rug Options") or alias.
        offset: First body line to return (0-based), for paging long notes.
        limit: Number of body lines to return (max 500).
        heading: Return only this heading's section (to the next heading of
            the same or a higher level); offset and limit page within it.
    """
    _s, v = _ctx()
    n = _find(v, note)
    text = v.read_text(n.path)
    _fm, body, ok = _notes.split(text)
    if heading:
        try:
            start, stop = _notes.section(body, heading)
        except KeyError as exc:
            found = [h for _, _, h in _notes.headings(body)][:100]
            raise ToolError(
                f"heading {heading!r} not found; headings: {found}"
            ) from exc
        body = "\n".join(body.split("\n")[start:stop])
    lines = body.split("\n")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    end = min(len(lines), offset + limit)
    notes = v.notes()
    links, unresolved = [], []
    files: set[str] | None = None
    for t in n.links:
        hit = v.resolve_link(t, notes)
        if hit:
            links.append(hit.path)
            continue
        files = v.files() if files is None else files
        att = v.attachment(t, files)
        (links if att else unresolved).append(att or t)
    out: dict[str, Any] = {
        "backend": FS,
        "path": n.path,
        "title": n.title,
        "version": Vault.version(text),
        "bytes": len(text.encode("utf-8")),
        "frontmatter": (
            _notes.typed(text) if ok else {"error": "frontmatter is not valid YAML"}
        ),
        "content": "\n".join(lines[offset:end]),
        "lines": f"{offset + 1}-{end} of {len(lines)}"
        + (f" in section {heading!r}" if heading else ""),
        "links": list(dict.fromkeys(links)),
        "backlinks": [b.path for b in v.backlinks(n)][:100],
    }
    if unresolved:
        out["unresolved_links"] = unresolved
    if end < len(lines):
        out["next_offset"] = end
    return out


def _person_matcher(v: Vault, person: str) -> Any:
    try:
        target: str | None = v.find(person).path
    except VaultError:
        target = None
    notes = v.notes()
    want = person.strip().lower()

    def match(n: Note) -> bool:
        for prop in ("people", "author"):
            for value in _notes.as_list(n.frontmatter.get(prop)):
                for t in _notes.wikilinks(value) or [value]:
                    hit = v.resolve_link(t, notes)
                    if (hit and hit.path == target) or t.strip().lower() == want:
                        return True
        return False

    return match


def _select(
    v: Vault,
    *,
    folder: str | None = None,
    type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    updated_before: str | None = None,
    updated_after: str | None = None,
) -> list[Note]:
    pmatch = _person_matcher(v, person) if person else None
    tag = tag.lstrip("#").lower() if tag else None
    out = []
    for n in v.notes():
        fm = n.frontmatter
        if folder and not (n.path.startswith(folder.strip("/") + "/")):
            continue
        if type and fm.get("type") != type:
            continue
        if status and fm.get("status") != status:
            continue
        if tag and tag not in n.tags:
            continue
        if pmatch and not pmatch(n):
            continue
        if updated_before or updated_after:
            when = str(fm.get("updated") or fm.get("created") or "")[:10]
            if not when:
                continue
            if updated_before and not when < updated_before:
                continue
            if updated_after and not when > updated_after:
                continue
        out.append(n)
    return sorted(out, key=lambda n: n.path)


def list_notes(
    folder: str | None = None,
    type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    updated_before: str | None = None,
    updated_after: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List notes filtered by folder and properties, e.g. "my active lists".

    Args:
        folder: Vault folder, recursive (e.g. "TTRPG/Murder on the Eberron Express/NPCs").
        type: note, list, recipe, journal, poem, message, reference, document,
            log, campaign, writing, person, index or meta.
        status: active, archive or draft.
        tag: A theme tag such as "eberron" (frontmatter or inline).
        person: A person's note title or alias; matches the ``people``/``author`` properties.
        updated_before: YYYY-MM-DD; compares ``updated``, else ``created``
            (notes with neither are left out).
        updated_after: YYYY-MM-DD, same rule.
        limit: Maximum notes to return (max 500).
    """
    _s, v = _ctx()
    found = _select(
        v,
        folder=folder,
        type=type,
        status=status,
        tag=tag,
        person=person,
        updated_before=updated_before,
        updated_after=updated_after,
    )
    limit = max(1, min(limit, 500))
    return {
        "backend": FS,
        "total": len(found),
        "notes": [n.summary() for n in found[:limit]],
    }


def get_links(note: str) -> dict[str, Any]:
    """Outgoing wikilinks of a note (body and properties), resolved to paths."""
    _s, v = _ctx()
    n = _find(v, note)
    notes = v.notes()
    files = v.files()
    out = []
    for t in n.links:
        hit = v.resolve_link(t, notes)
        out.append({"target": t, "path": hit.path if hit else v.attachment(t, files)})
    return {"backend": FS, "path": n.path, "links": out}


def get_backlinks(note: str) -> dict[str, Any]:
    """Notes that link to this one (body or properties such as ``up``/``people``)."""
    _s, v = _ctx()
    n = _find(v, note)
    return {
        "backend": FS,
        "path": n.path,
        "backlinks": [b.summary() for b in v.backlinks(n)],
    }


def list_tags() -> dict[str, Any]:
    """Every tag in use with its note count, plus the Vault Guide's theme tags."""
    _s, v = _ctx()
    counts = Counter(t for n in v.notes() for t in n.tags)
    return {
        "backend": FS,
        "tags": dict(counts.most_common()),
        "theme_tags": list(_notes.THEME_TAGS),
    }


def list_properties() -> dict[str, Any]:
    """Property names in use with counts, and the values of type/status/source."""
    _s, v = _ctx()
    notes = v.notes()
    names = Counter(k for n in notes for k in n.frontmatter)
    values = {
        key: dict(
            Counter(str(n.frontmatter[key]) for n in notes if n.frontmatter.get(key))
        )
        for key in ("type", "status", "source")
    }
    return {
        "backend": FS,
        "properties": dict(names.most_common()),
        "values": values,
        "allowed": {
            "type": list(_notes.TYPES),
            "status": list(_notes.STATUSES),
            "source": list(_notes.SOURCES),
        },
    }


_TASK_RE = re.compile(r"^\s*[-*+]\s+\[(.)\]\s+(.*\S)")


def _fs_tasks(v: Vault, folder: str | None) -> list[dict[str, Any]]:
    out = []
    for n in v.notes():
        if folder and not n.path.startswith(folder.strip("/") + "/"):
            continue
        for i, line in enumerate(v.read_text(n.path).split("\n")):
            m = _TASK_RE.match(line)
            if m:
                out.append(
                    {
                        "path": n.path,
                        "line": i + 1,
                        "status": m.group(1),
                        "text": m.group(2),
                    }
                )
    return out


def _cli_tasks(s: Settings, status: str) -> list[dict[str, Any]]:
    flags = {"todo": ("todo",), "done": ("done",), "all": ()}[status]
    data = _cli.run_json(s, "tasks", *flags, "verbose")
    if not isinstance(data, list):
        raise _cli.CliError("unexpected tasks output")
    out = []
    for item in data:
        if not isinstance(item, dict):
            raise _cli.CliError("unexpected tasks output")
        text = item.get("text") or item.get("task") or item.get("content")
        path = item.get("path") or item.get("file")
        if not (isinstance(text, str) and isinstance(path, str)):
            raise _cli.CliError("unexpected tasks output")
        # The CLI (1.12) gives the whole "- [ ] ..." line and the line number as
        # a string; normalize both to match _fs_tasks.
        m = _TASK_RE.match(text)
        line = str(item.get("line", ""))
        out.append(
            {
                "path": path,
                "line": int(line) if line.isdigit() else item.get("line"),
                "status": item.get("status", "x" if item.get("completed") else " "),
                "text": m.group(2) if m else text.strip(),
            }
        )
    return out


def tasks(
    status: str = "todo", folder: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """Checkbox tasks across the vault.

    Args:
        status: "todo" (open, the default), "done" or "all".
        folder: Only tasks in notes under this folder.
        limit: Maximum tasks to return (max 500).
    """
    s, v = _ctx()
    if status not in {"todo", "done", "all"}:
        raise ToolError("status must be todo, done or all")
    backend, note = CLI, None
    try:
        found = _cli_tasks(s, status)
        if folder:
            found = [t for t in found if t["path"].startswith(folder.strip("/") + "/")]
    except _cli.CliError as exc:
        backend, note = FS, f"read the files directly ({exc})"
        found = _fs_tasks(v, folder)
        if status == "todo":
            found = [t for t in found if t["status"] == " "]
        elif status == "done":
            found = [t for t in found if t["status"] != " "]
    limit = max(1, min(limit, 500))
    out: dict[str, Any] = {
        "backend": backend,
        "total": len(found),
        "tasks": found[:limit],
    }
    if note:
        out["note"] = note
    return out


def health() -> dict[str, Any]:
    """Server status: Obsidian app and CLI reachability, vault size, last change."""
    s = get_settings()
    v = _vault()
    out: dict[str, Any] = {
        "vault": str(s.vault_root),
        "vault_found": v.root.is_dir(),
        "readonly": s.readonly,
        "app_running": _cli.app_running(s),
    }
    try:
        _cli.run(s, "vault", info="name")
        out["cli"] = "ok"
    except _cli.CliError as exc:
        out["cli"] = str(exc)
    if out["vault_found"]:
        notes = v.notes()
        out["notes"] = len(notes)
        if notes:
            newest = max(notes, key=lambda n: n.mtime)
            out["last_change"] = {
                "path": newest.path,
                "at": datetime.fromtimestamp(newest.mtime, s.tz).isoformat(
                    timespec="seconds"
                ),
            }
    if out["vault_found"]:
        out["sync"] = _ready.state(s)
    out["ok"] = bool(out["vault_found"] and out["cli"] == "ok")
    return out


# --- daily notes ----------------------------------------------------------------


def _daily_path(v: Vault, day: date) -> tuple[str, dict[str, Any]]:
    cfg = v.obsidian_config("daily-notes.json")
    folder = str(cfg.get("folder") or "").strip("/")
    name = _notes.moment_format(day, str(cfg.get("format") or "YYYY-MM-DD"))
    return (f"{folder}/{name}.md" if folder else f"{name}.md"), cfg


def _daily_from_template(v: Vault, s: Settings, day: date, rel: str, cfg: dict) -> str:
    tpl_cfg = v.obsidian_config("templates.json")
    when = datetime.combine(day, datetime.now(s.tz).time())
    template = str(cfg.get("template") or "").strip()
    try:
        raw = v.read_text(v.note_path(template)) if template else None
    except (OSError, VaultError):
        raw = None
    if raw is None:
        raw = "---\ntype: journal\nstatus: active\ncreated: {{date}}\ntags: [daily]\n---\n"
    return _notes.render_template(
        raw,
        title=PurePosixPath(rel).stem,
        when=when,
        date_format=str(tpl_cfg.get("dateFormat") or "YYYY-MM-DD"),
        time_format=str(tpl_cfg.get("timeFormat") or "HH:mm"),
    )


def daily_note(day: str | None = None) -> dict[str, Any]:
    """Read the daily note for today (or ``day``), creating it from the Daily
    Note template if it doesn't exist (unless the server is read-only).

    Args:
        day: YYYY-MM-DD; defaults to today in the vault's time zone.
    """
    s, v = _ctx()
    try:
        d = date.fromisoformat(day) if day else s.today()
    except ValueError as exc:
        raise ToolError("day must be YYYY-MM-DD") from exc
    rel, cfg = _daily_path(v, d)
    path = v.resolve(rel, write=True)
    created, backend = False, FS
    if not path.exists():
        if s.readonly:
            return {"backend": FS, "path": rel, "exists": False}
        if d == s.today():
            try:
                _cli.run(s, "daily")
                if _wait_for(path.exists, 5):
                    created, backend = True, CLI
            except _cli.CliError:
                pass
        if not created:
            v.write_text(rel, _daily_from_template(v, s, d, rel, cfg), create=True)
            created = True
        _audit.record(s, "daily_note", [rel], "created daily note", backend=backend)
    out = read_note(rel, 0, 400)
    out.update(backend=backend, created=created)
    return out


# --- write tools ----------------------------------------------------------------


class Patch(TypedDict):
    find: NotRequired[str]
    find_regex: NotRequired[str]
    replace: str
    replace_all: NotRequired[bool]
    count: NotRequired[int]


def _apply_patches(text: str, patch: list[Patch]) -> str:
    """``text`` with each find/replace applied in order; refuses on a wrong match count."""
    for i, p in enumerate(patch):
        find, rx = p.get("find"), p.get("find_regex")
        if not find and not rx:
            raise ToolError(f"patch {i}: 'find' is empty; give find or find_regex")
        if find and rx:
            raise ToolError(f"patch {i}: give 'find' or 'find_regex', not both")
        if rx:
            if len(rx) > 1000:
                raise ToolError(f"patch {i}: find_regex is over 1000 characters")
            try:
                pat = re.compile(rx, re.M)
            except re.error as exc:
                raise ToolError(f"patch {i}: bad find_regex: {exc}") from exc
            hits = sum(1 for _ in pat.finditer(text))
        else:
            hits = text.count(find or "")
        want = p.get("count")
        if want is not None:
            if hits != want:
                raise ToolError(
                    f"patch {i}: {'find_regex' if rx else 'find'} matches {hits} "
                    f"times, not the expected {want}"
                )
        elif hits == 0 or (hits > 1 and not p.get("replace_all")):
            raise ToolError(
                f"patch {i}: '{'find_regex' if rx else 'find'}' occurs {hits} times; "
                "it must occur exactly once (or set replace_all, or count)"
            )
        if rx:
            try:
                text = pat.sub(p["replace"], text)
            except (re.error, IndexError) as exc:
                raise ToolError(f"patch {i}: bad replacement: {exc}") from exc
        else:
            text = text.replace(find or "", p["replace"])
    return text


@_locked
def create_note(
    title: str,
    body: str = "",
    folder: str = "_Inbox",
    type: str = "note",
    properties: dict[str, Any] | None = None,
    template: str | None = None,
    patch: list[Patch] | None = None,
    lock: str | None = None,
) -> dict[str, Any]:
    """Create a note following the Vault Guide, optionally from a template.

    New notes go to ``_Inbox`` unless a folder is given, and get ``type``,
    ``status: active``, ``created`` (today) and ``tags: []``; a note in a
    top-level area folder gets ``up`` pointing at that folder's overview note.
    Link people and notes with wikilinks (``[[Person Name]]``). Numbers and
    booleans in ``properties`` are written as such; a value whose type differs
    from the same property everywhere else in the vault is refused.

    Args:
        title: Note title (the filename); say what the note is, dates first
            for dated entries ("2026-11-11 Workout").
        body: Markdown body. An "# <title>" heading is added if it has none.
        folder: Vault folder, e.g. "Home" or "TTRPG/Murder on the Eberron Express/NPCs".
        type: One of the Vault Guide types (note, list, recipe, journal, ...).
        properties: Extra frontmatter, e.g. {"people": ["[[Person Name]]"],
            "up": "[[Murder on the Eberron Express]]", "tags": ["eberron"]}.
        template: A note in the templates folder, e.g. "Elden Ring – Weapon
            Item": its {{title}}/{{date}}/{{time}} are filled, ``properties``
            override its frontmatter and ``body`` is added after its body.
        patch: Find/replace edits on the new note's body (as in update_note),
            to fill in a template's placeholders.
        lock: The id from begin_write, when a lock covers this note.
    """
    s, v = _ctx()
    rel, text, fm, warnings = _new_note(
        s, v, title, body, folder, type, properties, template, patch
    )
    try:
        v.write_text(rel, text, create=True)
    except VaultError as exc:
        raise ToolError(str(exc)) from exc
    what = f"from template {template}" if template else f"({type})"
    _audit.record(s, "create_note", [rel], f"created {what}", backend=FS)
    out: dict[str, Any] = {"backend": FS, **_receipt(rel, text), "frontmatter": fm}
    if warnings:
        out["warnings"] = warnings
    return out


def _new_note(
    s: Settings,
    v: Vault,
    title: str,
    body: str = "",
    folder: str = "_Inbox",
    type: str = "note",
    properties: dict[str, Any] | None = None,
    template: str | None = None,
    patch: list[Patch] | None = None,
) -> tuple[str, str, dict[str, Any], list[str]]:
    """``(path, text, frontmatter, warnings)`` for a new note, checked but not written."""
    try:
        title = v.clean_title(title)
        folder = folder.strip().rstrip("/")
        rel = f"{folder}/{title}.md" if folder else f"{title}.md"
        if v.resolve(rel, write=True).exists():
            raise VaultError(f"{rel} already exists")
    except VaultError as exc:
        raise ToolError(str(exc)) from exc
    if template:
        text = _from_template(s, v, template, title, type, properties, body)
    else:
        fm: dict[str, Any] = {
            "type": type,
            "status": "active",
            "created": _today_str(s),
            "tags": [],
        }
        top = folder.split("/")[0] if folder else ""
        if top and not top.startswith("_") and "/" not in folder:
            if v.resolve(f"{top}/{top}.md").is_file():
                fm["up"] = f"[[{top}]]"
        fm.update(properties or {})
        if not body.lstrip().startswith("# "):
            body = f"# {title}\n\n{body}" if body.strip() else f"# {title}\n"
        if not body.endswith("\n"):
            body += "\n"
        text = _notes.join(fm, body)
    if patch:
        head, cur = _notes.split_raw(text)
        text = head + _apply_patches(cur, patch)
    fm = _notes.typed(text)
    warnings = _check_vocab(fm)
    _check_types(v, fm)
    return rel, text, fm, warnings


def _template_path(v: Vault, name: str) -> str:
    folder = str(v.obsidian_config("templates.json").get("folder") or "_Templates")
    folder = folder.strip("/")
    want = name.strip().removesuffix(".md")
    candidates = [f"{want}.md", f"{folder}/{want}.md"]
    for rel in candidates:
        try:
            if v.resolve(rel).is_file():
                return rel
        except VaultError:
            continue
    inside = [n for n in v.notes() if n.path.startswith(folder + "/")]
    hits = [n.path for n in inside if n.title.lower() == want.lower()]
    if len(hits) == 1:
        return hits[0]
    raise ToolError(
        f"no template {name!r}; templates: {[n.title for n in inside][:50]}"
    )


def _from_template(
    s: Settings,
    v: Vault,
    template: str,
    title: str,
    type: str,
    properties: dict[str, Any] | None,
    body: str,
) -> str:
    """A new note's text from a template, with the Vault Guide's required properties."""
    cfg = v.obsidian_config("templates.json")
    text = _notes.render_template(
        v.read_text(_template_path(v, template)),
        title=title,
        when=datetime.now(s.tz),
        date_format=cfg.get("dateFormat") or "YYYY-MM-DD",
        time_format=cfg.get("timeFormat") or "HH:mm",
    )
    fm, _body, ok = _notes.split(text)
    if not ok:
        raise ToolError(f"template {template!r} has invalid frontmatter")
    defaults = {"type": type, "status": "active", "created": _today_str(s), "tags": []}
    changes = {k: val for k, val in defaults.items() if fm.get(k) in (None, "")}
    changes.update(properties or {})
    if changes:
        text = _notes.set_properties(text, changes)
    if body.strip():
        text = text.rstrip("\n") + "\n\n" + body.strip("\n") + "\n"
    return text


@_locked
def update_note(
    note: str,
    body: str | None = None,
    patch: list[Patch] | None = None,
    properties: dict[str, Any] | None = None,
    if_version: str | None = None,
    lock: str | None = None,
) -> dict[str, Any]:
    """Edit a note: replace its body, apply find/replace patches, and/or merge properties.

    The frontmatter is only changed through ``properties``, so body edits can't
    break it. ``updated`` is not touched (it records an import's last edit).

    Args:
        note: Vault path, title or alias.
        body: New body text (everything below the frontmatter).
        patch: Find/replace edits on the body, applied in order: each has
            ``find`` (literal) or ``find_regex`` (Python regex, multiline;
            ``replace`` may use \\1 or \\g<name>). A match must occur exactly
            once, unless ``replace_all`` is true or ``count`` gives the exact
            number of matches expected (all are replaced).
        properties: Frontmatter keys to set; a null value removes the key.
            Lists replace whole lists, e.g. {"aliases": ["Old", "Older"]}.
            Numbers and booleans are written as such; text that looks like a
            number is quoted. A type that conflicts with the vault is refused.
        if_version: The ``version`` from read_note; refuses (returning the
            current text) if the note changed since.
        lock: The id from begin_write, when a lock covers this note.
    """
    s, v = _ctx()
    n, text, new, warnings, what = _edit_note(
        v, note, body, patch, properties, if_version
    )
    changed = new != text
    if changed:
        v.write_text(n.path, new, expect=text)
        _audit.record(
            s, "update_note", [n.path], f"updated {', '.join(what)}", backend=FS
        )
    out: dict[str, Any] = {"backend": FS, **_receipt(n.path, new), "changed": changed}
    if warnings:
        out["warnings"] = warnings
    return out


def _edit_note(
    v: Vault,
    note: str,
    body: str | None = None,
    patch: list[Patch] | None = None,
    properties: dict[str, Any] | None = None,
    if_version: str | None = None,
) -> tuple[Note, str, str, list[str], list[str]]:
    """``(note, old text, new text, warnings, what changed)``, not written."""
    if body is None and not patch and not properties:
        raise ToolError("give body, patch or properties")
    n = _find(v, note)
    text = v.read_text(n.path)
    _check_version(n.path, text, if_version)
    head, cur = _notes.split_raw(text)
    if body is not None:
        cur = body if body.endswith("\n") else body + "\n"
    if patch:
        cur = _apply_patches(cur, patch)
    new = head + cur
    warnings: list[str] = []
    if properties:
        warnings = _check_vocab(properties)
        _check_types(v, properties, n.path)
        try:
            new = _notes.set_properties(new, properties)
        except _notes.FrontmatterError as exc:
            raise ToolError(str(exc)) from exc
    what = [
        k
        for k, x in (("body", body), ("patch", patch), ("properties", properties))
        if x
    ]
    return n, text, new, warnings, what


@_locked
def append_to_note(
    note: str,
    text: str,
    heading: str | None = None,
    if_version: str | None = None,
    lock: str | None = None,
) -> dict[str, Any]:
    """Append text to the end of a note, or to the end of one heading's section.

    For today's daily note use ``daily_note`` first (it creates the note), then
    e.g. append_to_note(path, "- buy cat litter", heading="Capture"). An empty
    template bullet ("- ") at the end of the section is replaced.

    Args:
        note: Vault path, title or alias.
        text: Markdown to add; include list markers ("- ", "- [ ] ") yourself.
        heading: Heading text (without #s) whose section to append to.
        if_version: The ``version`` from read_note; refuses (returning the
            current text) if the note changed since.
        lock: The id from begin_write, when a lock covers this note.
    """
    s, v = _ctx()
    n = _find(v, note)
    raw = v.read_text(n.path)
    _check_version(n.path, raw, if_version)
    head, body = _notes.split_raw(raw)
    try:
        body = _notes.append_under_heading(body, text, heading)
    except KeyError as exc:
        found = [h for _, _, h in _notes.headings(body)]
        raise ToolError(f"heading {heading!r} not found; headings: {found}") from exc
    v.write_text(n.path, head + body, expect=raw)
    where = f" under '{heading}'" if heading else ""
    _audit.record(
        s, "append_to_note", [n.path], f"appended {len(text)} chars{where}", backend=FS
    )
    return {
        "backend": FS,
        **_receipt(n.path, head + body),
        "appended": text,
        "heading": heading,
    }


def _target_path(v: Vault, n: Note, new: str) -> str:
    new = new.strip()
    if new.endswith("/"):
        return f"{new.strip('/')}/{n.title}.md".lstrip("/")
    if "/" in new:
        parts = new.removesuffix(".md").split("/")
        return "/".join([*parts[:-1], v.clean_title(parts[-1])]) + ".md"
    folder = n.folder
    title = v.clean_title(new.removesuffix(".md"))
    return f"{folder}/{title}.md" if folder else f"{title}.md"


def _fs_relink(
    v: Vault, old: Note, notes: list[Note], affected: list[Note], new_rel: str
) -> int:
    """Rewrite links to ``old`` (resolved against the pre-move ``notes``) to ``new_rel``."""
    target = v.link_text(new_rel, [x for x in notes if x.path != old.path])
    total = 0
    for a in affected:
        path = new_rel if a.path == old.path else a.path
        text = v.read_text(path)

        def points_at_old(t: str) -> bool:
            hit = v.resolve_link(t, notes)
            return hit is not None and hit.path == old.path

        new_text, count = _notes.rewrite_links(text, points_at_old, target)
        if count:
            v.write_text(path, new_text)
            total += count
    return total


@_locked
def move_note(
    note: str,
    new_path_or_title: str,
    dry_run: bool | None = None,
    lock: str | None = None,
) -> dict[str, Any]:
    """Rename or move a note, updating every link to it, and keep the old title as an alias.

    Args:
        note: Vault path, title or alias of the note to move.
        new_path_or_title: A new title ("Living Room Rug Options", same folder),
            a folder ending in "/" ("Home/"), or a full path ("Home/Rugs/Rug Options").
        dry_run: true returns the plan only; false applies it. By default it
            applies unless more than ~10 notes would change, in which case it
            returns the plan and you call again with dry_run=false.
        lock: The id from begin_write, when a lock covers these notes.
    """
    s, v = _ctx()
    mv = _plan_move(v, note, new_path_or_title)
    if dry_run or (dry_run is None and len(mv.linkers) + 1 > s.bulk_threshold):
        return {"dry_run": True, **mv.plan(), "apply_with": "dry_run=false"}
    backend, note_txt = _apply_move(s, v, mv)
    out: dict[str, Any] = {"backend": backend, "dry_run": False, **mv.plan()}
    if note_txt:
        out["note"] = note_txt
    return out


@dataclass
class _Move:
    n: Note
    new_rel: str
    notes: list[Note]  # the index before the move, to resolve old links
    linkers: list[Note]
    alias: str | None

    def plan(self) -> dict[str, Any]:
        return {
            "from": self.n.path,
            "to": self.new_rel,
            "alias_added": self.alias,
            "notes_with_links_to_update": [b.path for b in self.linkers],
        }


def _plan_move(v: Vault, note: str, new_path_or_title: str) -> _Move:
    n = _find(v, note)
    try:
        new_rel = _target_path(v, n, new_path_or_title)
        new_abs = v.resolve(new_rel, write=True)
    except VaultError as exc:
        raise ToolError(str(exc)) from exc
    if new_rel == n.path:
        raise ToolError("the note is already there")
    if new_abs.exists():
        raise ToolError(f"{new_rel} already exists")
    new_title = PurePosixPath(new_rel).stem
    alias = n.title if new_title != n.title and n.title not in n.aliases else None
    return _Move(n, new_rel, v.notes(), v.backlinks(n), alias)


def _apply_move(s: Settings, v: Vault, mv: _Move) -> tuple[str, str | None]:
    """Move the note (CLI, else the files) and fix links; ``(backend, note)``."""
    n, new_rel, notes, linkers = mv.n, mv.new_rel, mv.notes, mv.linkers
    _lock_check(s, n.path, new_rel, *(b.path for b in linkers))
    new_abs = v.resolve(new_rel, write=True)
    if mv.alias:
        v.write_text(
            n.path,
            _notes.set_properties(
                v.read_text(n.path), {"aliases": [*n.aliases, mv.alias]}
            ),
        )
    backend, note_txt = CLI, None
    try:
        if PurePosixPath(new_rel).parent == PurePosixPath(n.path).parent:
            _cli.run(s, "rename", path=n.path, name=PurePosixPath(new_rel).stem)
        else:
            _cli.run(s, "move", path=n.path, to=new_rel)
        if not _wait_for(lambda: new_abs.is_file() and not v.resolve(n.path).exists()):
            raise _cli.CliError("Obsidian did not move the file")

        # Obsidian rewrites links itself when "Automatically update internal
        # links" is on; give it a moment, then fix whatever it left behind.
        def not_relinked() -> list[Note]:
            current = {x.path for x in v.backlinks(v.find(new_rel))}
            return [b for b in linkers if b.path not in current]

        _wait_for(lambda: not not_relinked(), 3)
        if left := not_relinked():
            fixed = _fs_relink(v, n, notes, left, new_rel)
            note_txt = f"fixed {fixed} links Obsidian left unchanged"
    except _cli.CliError as exc:
        if v.resolve(n.path).exists():
            backend = FS
            v.move_file(n.path, new_rel)
            fixed = _fs_relink(v, n, notes, [*linkers, n], new_rel)
            note_txt = f"Obsidian unavailable ({exc}); moved the file and rewrote {fixed} links"
        else:
            raise ToolError(f"move may be incomplete: {exc}") from exc
    _audit.record(
        s,
        "move_note",
        [n.path, new_rel],
        f"moved; {len(linkers)} linking notes",
        backend=backend,
    )
    return backend, note_txt


@_locked
def delete_note(note: str, lock: str | None = None) -> dict[str, Any]:
    """Delete a note to Obsidian's trash (never asks for a permanent delete).

    Obsidian follows the vault's "Deleted files" setting, which should be
    ".trash folder" (see README). If Obsidian is down the note is moved to
    ``_Review/To delete/`` instead.

    Args:
        note: Vault path, title or alias.
        lock: The id from begin_write, when a lock covers this note.
    """
    s, v = _ctx()
    n = _find(v, note)
    _lock_check(s, n.path)
    linkers = [b.path for b in v.backlinks(n)]
    path = v.resolve(n.path, write=True)
    try:
        _cli.run(s, "delete", path=n.path)
        if not _wait_for(lambda: not path.exists()):
            raise _cli.CliError("Obsidian did not delete the file")
        backend, dest = CLI, "Obsidian trash"
    except _cli.CliError:
        if not path.exists():
            raise
        backend = FS
        dest = f"_Review/To delete/{n.path}"
        if v.resolve(dest).exists():
            dest = dest.removesuffix(".md") + f" {datetime.now(s.tz):%Y%m%d-%H%M%S}.md"
        v.move_file(n.path, dest)
    _audit.record(s, "delete_note", [n.path], f"deleted to {dest}", backend=backend)
    out: dict[str, Any] = {"backend": backend, "path": n.path, "moved_to": dest}
    if linkers:
        out["warning"] = f"{len(linkers)} notes still link to it: {linkers[:20]}"
    return out


def _snapshot(s: Settings, message: str) -> str:
    """Run the snapshot command (a local git commit of the vault); report, don't fail."""
    try:
        proc = subprocess.run(  # noqa: S603 -- operator-configured command
            [*shlex.split(s.snapshot_cmd), message],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"failed: {exc}"
    return "ok" if proc.returncode == 0 else f"failed: {proc.stderr.strip()[:200]}"


def _where(n: Note, where: dict[str, Any]) -> bool:
    """Whether ``n``'s properties equal ``where``'s (a list property contains the value;
    null means missing or empty)."""
    for key, want in where.items():
        have = n.frontmatter.get(key)
        if want is None:
            if have not in (None, "", []):
                return False
        elif isinstance(have, list):
            if str(want) not in have and not (
                isinstance(want, list) and [str(w) for w in want] == have
            ):
                return False
        elif isinstance(want, bool):
            if str(have).lower() != str(want).lower():
                return False
        elif have is None or str(have) != str(want):
            return False
    return True


MAX_BULK = 500


@_locked
def bulk_update(
    set_properties: dict[str, Any] | None = None,
    patch: list[Patch] | None = None,
    folder: str | None = None,
    type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    updated_before: str | None = None,
    updated_after: str | None = None,
    where: dict[str, Any] | None = None,
    query: str | None = None,
    paths: list[str] | None = None,
    dry_run: bool = True,
    lock: str | None = None,
) -> dict[str, Any]:
    """Set properties on, and/or patch the body of, every note matching a selector.

    Always returns a plan first (dry_run=true) with a count and a short diff
    per note. Show it to the user, then call again with the same arguments and
    dry_run=false. All or nothing: if a patch doesn't match in some note, or a
    property type conflicts, nothing is written. Example: archive lists not
    updated since 2023 -> set_properties={"status": "archive"}, type="list",
    status="active", updated_before="2023-01-01". To make different edits to
    different notes, use update_notes.

    Args:
        set_properties: Frontmatter keys to set; null removes the key.
        patch: Find/replace edits applied to each note's body, as in
            update_note (``find`` or ``find_regex``; ``count`` asserts the
            matches per note). Narrow the selection with ``query`` so every
            note contains the text.
        folder, type, status, tag, person, updated_before, updated_after:
            Filters, as in list_notes.
        where: Property values to match, e.g. {"catalogue": "weapon",
            "batch": 12}; a list property matches if it contains the value;
            null matches a missing or empty property.
        query: Only notes containing all of these words (plain text search).
        paths: Explicit notes (paths or titles) instead of, or as well as, filters.
        dry_run: true (default) returns the plan; false applies it.
        lock: The id from begin_write, when a lock covers these notes.
    """
    s, v = _ctx()
    if not set_properties and not patch:
        raise ToolError("give set_properties and/or patch")
    filters = {
        "folder": folder,
        "type": type,
        "status": status,
        "tag": tag,
        "person": person,
        "updated_before": updated_before,
        "updated_after": updated_after,
    }
    if not paths and not any(filters.values()) and not where and not query:
        raise ToolError(
            "give at least one filter, where, query or paths; refusing to touch every note"
        )
    warnings = _check_vocab(set_properties or {})
    targets = _select(v, **filters) if any(filters.values()) else v.notes()
    if where:
        targets = [n for n in targets if _where(n, where)]
    if query:
        hits = {n.path for n in _fs_search(v, _terms(query), folder)}
        targets = [n for n in targets if n.path in hits]
    if paths:
        explicit = {_find(v, x).path for x in paths}
        narrowed = any(filters.values()) or where or query
        targets = (
            [t for t in targets if t.path in explicit]
            if narrowed
            else [_find(v, x) for x in sorted(explicit)]
        )
    targets = sorted(targets, key=lambda n: n.path)
    if len(targets) > MAX_BULK:
        raise ToolError(
            f"{len(targets)} notes match; at most {MAX_BULK} per call, so narrow the selection"
        )
    plan: list[dict[str, Any]] = []
    edits: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    errors: list[dict[str, Any]] = []
    for n in targets:
        if set_properties and not n.frontmatter_ok:
            skipped.append(n.path)
            continue
        old = v.read_text(n.path)
        try:
            new = old
            if patch:
                head, cur = _notes.split_raw(new)
                new = head + _apply_patches(cur, patch)
            if set_properties:
                _check_types(v, set_properties, n.path)
                new = _notes.set_properties(new, set_properties)
        except (ToolError, _notes.FrontmatterError) as exc:
            errors.append({"path": n.path, "error": str(exc)})
            continue
        if new == old:
            continue
        edits.append((n.path, old, new))
        item: dict[str, Any] = {"path": n.path}
        if set_properties:
            item["properties"] = {
                k: [n.frontmatter.get(k), val]
                for k, val in set_properties.items()
                if n.frontmatter.get(k) != val
            }
        if patch:
            item["diff"] = _diff(old, new, max_lines=12)
        plan.append(item)
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "matched": len(targets),
        "to_change": len(edits),
        "plan": plan[:_PLAN_SHOWN],
    }
    if len(plan) > _PLAN_SHOWN:
        out["plan_note"] = f"showing {_PLAN_SHOWN} of {len(plan)} notes"
    if skipped:
        out["skipped_invalid_frontmatter"] = skipped
    if warnings:
        out["warnings"] = warnings
    if errors:
        out.update(dry_run=True, errors=errors[:_PLAN_SHOWN])
        out["note"] = (
            f"nothing was written: {len(errors)} notes failed; fix the patch or "
            "narrow the selection (e.g. with query)"
        )
        return out
    if dry_run:
        out["apply_with"] = "the same arguments and dry_run=false"
        return out
    if edits and s.snapshot_cmd:
        out["snapshot"] = _snapshot(s, f"before bulk_update on {len(edits)} notes")
    written: list[dict[str, Any]] = []
    try:
        for rel, old, new in edits:
            v.write_text(rel, new, expect=old)
            written.append(_receipt(rel, new))
    except VaultError as exc:
        raise ToolError(
            f"stopped after {len(written)} of {len(edits)} notes ({exc}); "
            "the rest are unchanged, so plan them again"
        ) from exc
    finally:
        if written:
            what = ", ".join(
                x for x, on in (("properties", set_properties), ("patch", patch)) if on
            )
            _audit.record(
                s,
                "bulk_update",
                [w["path"] for w in written],
                f"updated {what} on {len(written)} notes",
                backend=FS,
            )
    out.update(backend=FS, receipts=written)
    return out


# --- batch tools ----------------------------------------------------------------

MAX_BATCH = 200
# Applying stops starting new moves after this, so the reply gets back inside
# the front end's 25 s call timeout; the rest come back as ``remaining``.
_BATCH_SECONDS = 15.0
_PLAN_SHOWN = 50


class NewNote(TypedDict):
    title: str
    body: NotRequired[str]
    folder: NotRequired[str]
    type: NotRequired[str]
    properties: NotRequired[dict[str, Any]]
    template: NotRequired[str]
    patch: NotRequired[list[Patch]]


class NoteEdit(TypedDict):
    note: str
    body: NotRequired[str]
    patch: NotRequired[list[Patch]]
    properties: NotRequired[dict[str, Any]]
    if_version: NotRequired[str]


class Move(TypedDict):
    note: str
    to: str


def _check_batch(items: list[Any], what: str) -> None:
    if not items:
        raise ToolError(f"{what} is empty")
    if len(items) > MAX_BATCH:
        raise ToolError(f"at most {MAX_BATCH} {what} per call; split the batch")


def _refuse(out: dict[str, Any], errors: list[dict[str, Any]]) -> dict[str, Any]:
    out.update(
        dry_run=True,
        errors=errors,
        note="nothing was written; fix these items and call again",
    )
    return out


def _diff(old: str, new: str, max_lines: int = 40) -> str:
    lines = list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1)
    )[2:]
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"... {len(lines) - max_lines} more diff lines"]
    return "\n".join(lines)


@_locked
def create_notes(
    notes: list[NewNote], dry_run: bool | None = None, lock: str | None = None
) -> dict[str, Any]:
    """Create many notes in one call, each following the Vault Guide as in create_note.

    Every note is checked first (title, folder, vocabulary, no existing file,
    no duplicate in the batch); if any fails, nothing is written and the errors
    come back. For files that already exist somewhere as a zip or tar, use
    import_archive instead.

    Args:
        notes: Up to 200 notes, each {"title", "body"?, "folder"? (default
            "_Inbox"), "type"? (default "note"), "properties"?, "template"?,
            "patch"?} with create_note's meanings.
        dry_run: true returns the plan only; false creates. By default it
            creates up to ~10 notes and returns the plan for more; then call
            again with the same notes and dry_run=false.
        lock: The id from begin_write, when a lock covers these notes.
    """
    s, v = _ctx()
    _check_batch(notes, "notes")
    planned: list[tuple[str, str]] = []
    errors: list[dict[str, Any]] = []
    warnings: dict[str, list[str]] = {}
    for i, spec in enumerate(notes):
        try:
            rel, text, _fm, warn = _new_note(
                s,
                v,
                spec["title"],
                spec.get("body", ""),
                spec.get("folder", "_Inbox"),
                spec.get("type", "note"),
                spec.get("properties"),
                spec.get("template"),
                spec.get("patch"),
            )
            if any(rel.lower() == p.lower() for p, _ in planned):
                raise ToolError(f"{rel} appears twice in this batch")
        except ToolError as exc:
            errors.append({"index": i, "title": spec.get("title"), "error": str(exc)})
            continue
        planned.append((rel, text))
        if warn:
            warnings[rel] = warn
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "to_create": len(planned),
        "paths": [p for p, _ in planned],
    }
    if warnings:
        out["warnings"] = warnings
    if errors:
        return _refuse(out, errors)
    if dry_run or (dry_run is None and len(planned) > s.bulk_threshold):
        out.update(dry_run=True, apply_with="the same notes and dry_run=false")
        return out
    created: list[str] = []
    receipts: list[dict[str, Any]] = []
    try:
        for rel, text in planned:
            v.write_text(rel, text, create=True)
            created.append(rel)
            receipts.append(_receipt(rel, text))
    except VaultError as exc:
        raise ToolError(
            f"stopped after creating {len(created)} of {len(planned)} notes: {exc}"
        ) from exc
    finally:
        if created:
            _audit.record(
                s, "create_notes", created, f"created {len(created)} notes", backend=FS
            )
    out.update(dry_run=False, backend=FS, receipts=receipts)
    return out


@_locked
def update_notes(
    updates: list[NoteEdit], dry_run: bool = True, lock: str | None = None
) -> dict[str, Any]:
    """Edit many notes in one call: each gets a new body, find/replace patches
    and/or properties, exactly as in update_note.

    Always returns a plan first (dry_run=true) with a short diff per note. Show
    it to the user, then call again with the same updates and dry_run=false.
    Nothing is written if any update fails its checks. To set the same
    properties on many notes, bulk_update is simpler.

    Args:
        updates: Up to 200 edits, each {"note", "body"?, "patch"?,
            "properties"?, "if_version"?} with the meanings of update_note's
            arguments; one edit per note.
        dry_run: true (default) returns the plan; false applies it.
        lock: The id from begin_write, when a lock covers these notes.
    """
    s, v = _ctx()
    _check_batch(updates, "updates")
    edits: list[tuple[str, str, str]] = []
    plan: list[dict[str, Any]] = []
    unchanged: list[str] = []
    errors: list[dict[str, Any]] = []
    for i, u in enumerate(updates):
        try:
            n, old, new, warn, what = _edit_note(
                v,
                u["note"],
                u.get("body"),
                u.get("patch"),
                u.get("properties"),
                u.get("if_version"),
            )
            if any(n.path == p for p, _, _ in edits) or n.path in unchanged:
                raise ToolError(f"{n.path} is edited twice in this batch; combine them")
        except ToolError as exc:
            errors.append({"index": i, "note": u.get("note"), "error": str(exc)})
            continue
        if new == old:
            unchanged.append(n.path)
            continue
        edits.append((n.path, old, new))
        item: dict[str, Any] = {
            "path": n.path,
            "changes": what,
            "diff": _diff(old, new),
        }
        if warn:
            item["warnings"] = warn
        plan.append(item)
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "to_change": len(edits),
        "plan": plan[:_PLAN_SHOWN],
    }
    if len(plan) > _PLAN_SHOWN:
        out["plan_note"] = f"showing {_PLAN_SHOWN} of {len(plan)} notes"
    if unchanged:
        out["unchanged"] = unchanged
    if errors:
        return _refuse(out, errors)
    if dry_run:
        out["apply_with"] = "the same updates and dry_run=false"
        return out
    if edits and s.snapshot_cmd:
        out["snapshot"] = _snapshot(s, f"before update_notes on {len(edits)} notes")
    written: list[str] = []
    receipts: list[dict[str, Any]] = []
    try:
        for path, old, new in edits:
            v.write_text(path, new, expect=old)
            written.append(path)
            receipts.append(_receipt(path, new))
    except VaultError as exc:
        raise ToolError(
            f"stopped after {len(written)} of {len(edits)} notes ({exc}); "
            "the rest are unchanged, so plan them again"
        ) from exc
    finally:
        if written:
            _audit.record(
                s, "update_notes", written, f"updated {len(written)} notes", backend=FS
            )
    out.update(backend=FS, receipts=receipts)
    return out


@_locked
def move_notes(
    moves: list[Move], dry_run: bool = True, lock: str | None = None
) -> dict[str, Any]:
    """Rename or move many notes, each as in move_note: links to it are updated
    and the old title is kept as an alias.

    Always returns a plan first; show it, then call again with the same moves
    and dry_run=false. Applying runs for at most ~15 s; anything not reached
    comes back as ``remaining``: call move_notes(moves=remaining, dry_run=false).

    Args:
        moves: Up to 200 {"note", "to"}; ``note`` is a path, title or alias and
            ``to`` is a new title, a folder ending in "/" or a full path.
        dry_run: true (default) returns the plan; false applies it.
        lock: The id from begin_write, when a lock covers these notes.
    """
    s, v = _ctx()
    _check_batch(moves, "moves")
    planned: list[tuple[Move, _Move]] = []
    errors: list[dict[str, Any]] = []
    for i, m in enumerate(moves):
        try:
            mv = _plan_move(v, m["note"], m["to"])
            if any(p.n.path == mv.n.path for _, p in planned):
                raise ToolError(f"{mv.n.path} is moved twice in this batch")
            if any(p.new_rel.lower() == mv.new_rel.lower() for _, p in planned):
                raise ToolError(f"two notes would move to {mv.new_rel}")
        except ToolError as exc:
            errors.append({"index": i, "note": m.get("note"), "error": str(exc)})
            continue
        planned.append(({"note": mv.n.path, "to": m["to"]}, mv))
    out: dict[str, Any] = {"dry_run": dry_run, "to_move": len(planned)}
    if errors or dry_run:
        out["plan"] = [mv.plan() for _, mv in planned[:_PLAN_SHOWN]]
        if len(planned) > _PLAN_SHOWN:
            out["plan_note"] = f"showing {_PLAN_SHOWN} of {len(planned)} moves"
    if errors:
        return _refuse(out, errors)
    if dry_run:
        out["apply_with"] = "the same moves and dry_run=false"
        return out
    if s.snapshot_cmd:
        out["snapshot"] = _snapshot(s, f"before move_notes on {len(planned)} notes")
    deadline = time.monotonic() + _BATCH_SECONDS
    done: list[dict[str, Any]] = []
    for k, (m, _) in enumerate(planned):
        if time.monotonic() > deadline:
            out["remaining"] = [m for m, _ in planned[k:]]
            out["next"] = "call move_notes(moves=remaining, dry_run=false)"
            break
        try:
            # Plan again: the moves before this one changed paths and links.
            mv = _plan_move(v, m["note"], m["to"])
            backend, note_txt = _apply_move(s, v, mv)
        except (ToolError, VaultError) as exc:
            out["failed"] = {**m, "error": str(exc)}
            out["remaining"] = [m for m, _ in planned[k + 1 :]]
            break
        item = {"from": mv.n.path, "to": mv.new_rel, "backend": backend}
        if note_txt:
            item["note"] = note_txt
        done.append(item)
    out["moved"] = done
    return out


def _with_defaults(s: Settings, raw: bytes) -> tuple[bytes, str | None]:
    """An imported note with the Vault Guide's required properties added where missing."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, "not UTF-8; imported as is"
    fm, _body, ok = _notes.split(text)
    if not ok:
        return raw, "frontmatter isn't valid YAML; imported as is"
    defaults = {
        "type": "note",
        "status": "active",
        "created": _today_str(s),
        "tags": [],
    }
    missing = {k: val for k, val in defaults.items() if k not in fm}
    if not missing:
        return raw, None
    return _notes.set_properties(text, missing).encode("utf-8"), None


@_locked
def import_archive(
    url: str,
    folder: str = "_Inbox",
    add_properties: bool = True,
    strip_top_folder: bool = True,
    sha256: str | None = None,
    dry_run: bool = True,
    lock: str | None = None,
) -> dict[str, Any]:
    """Import a zip or tar of notes and attachments from an https URL into a
    vault folder, keeping the archive's folder structure.

    The way to bring in many existing files at once. The URL must download the
    archive directly, e.g. a Dropbox link with dl=1, a temporary Dropbox
    download link, or a code-hosting archive or release asset.
    Always returns a plan first; call again with dry_run=false and the plan's
    sha256, so what's imported is exactly what was planned. Existing files are
    never overwritten (listed as conflicts and skipped). Hidden files
    (.obsidian, __MACOSX) and files over 5 MB are skipped. Limits: 100 MiB
    download, 2000 files.

    Args:
        url: https URL of a .zip, .tar, .tar.gz/.tgz, .tar.bz2 or .tar.xz.
        folder: Vault folder to import into, e.g. "_Inbox/Recipe import";
            "" for the vault root.
        add_properties: Add the Vault Guide's type: note, status: active,
            created (today) and tags: [] to imported notes where missing;
            existing properties are kept.
        strip_top_folder: Drop a single folder that wraps everything (as in
            downloaded or Finder-made archives); the plan names it.
        sha256: The plan's sha256; required with dry_run=false.
        dry_run: true (default) returns the plan; false imports.
        lock: The id from begin_write, when a lock covers the folder.
    """
    s, v = _ctx()
    folder = folder.strip().strip("/")
    try:
        if folder:
            v.resolve(folder, write=True)
        data = _archive.fetch(url)
        files, skipped = _archive.members(data)
    except (_archive.ArchiveError, VaultError) as exc:
        raise ToolError(str(exc)) from exc
    digest = hashlib.sha256(data).hexdigest()
    if not dry_run and sha256 != digest:
        raise ToolError(
            "give the sha256 from this archive's plan (call with dry_run=true first)"
            if not sha256
            else "the archive at that URL changed since it was planned; plan again"
        )
    stripped = _archive.strip_common_folder(files) if strip_top_folder else None
    writes: list[tuple[str, _archive.Member]] = []
    conflicts: list[str] = []
    for f in files:
        dest = f"{folder}/{f.path}" if folder else f.path
        try:
            exists = v.resolve(dest, write=True).exists()
        except VaultError as exc:
            skipped.append({"path": f.path, "reason": str(exc)})
            continue
        if exists or any(d.lower() == dest.lower() for d, _ in writes):
            conflicts.append(dest)
        else:
            writes.append((dest, f))
    n_notes = sum(d.lower().endswith(".md") for d, _ in writes)
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "sha256": digest,
        "to_write": len(writes),
        "notes": n_notes,
        "attachments": len(writes) - n_notes,
        "bytes": sum(f.size for _, f in writes),
        "paths": [d for d, _ in writes[:100]],
    }
    if len(writes) > 100:
        out["paths_note"] = f"showing 100 of {len(writes)} paths"
    if stripped:
        out["dropped_top_folder"] = stripped
    if conflicts:
        out["conflicts_skipped"] = conflicts[:100]
    if skipped:
        out["skipped"] = skipped[:100]
    if dry_run:
        out["apply_with"] = f"the same arguments, dry_run=false and sha256={digest!r}"
        return out
    written: list[str] = []
    warnings: list[str] = []
    try:
        for dest, f in writes:
            raw = f.read()
            if add_properties and dest.lower().endswith(".md"):
                raw, warn = _with_defaults(s, raw)
                if warn:
                    warnings.append(f"{dest}: {warn}")
            v.write_bytes(dest, raw)
            written.append(dest)
    except (VaultError, _archive.ArchiveError) as exc:
        raise ToolError(
            f"stopped after {len(written)} of {len(writes)} files: {exc}"
        ) from exc
    finally:
        if written:
            _audit.record(
                s,
                "import_archive",
                written,
                f"imported {len(written)} files from {url[:200]}",
                backend=FS,
                sha256=digest,
            )
    if warnings:
        out["warnings"] = warnings[:100]
    out["backend"] = FS
    return out


# --- byte-exact files ---------------------------------------------------------------

MAX_READ = 1_000_000
# Obsidian Sync Standard won't sync a bigger file.
MAX_WRITE = _archive.MAX_FILE
_STAGE_HOURS = 24


def read_file(path: str, offset: int = 0, length: int = MAX_READ) -> dict[str, Any]:
    """Read any vault file exactly as stored, with its sha256.

    Unlike read_note nothing is parsed: frontmatter and line endings come back
    as they are. Text comes back as UTF-8; anything else as base64. Long files
    come in chunks: call again with ``next_offset``.

    Args:
        path: Vault path, e.g. "Gaming/Elden Ring/Lore/Weapons/Full Text/x.md".
        offset: First byte to return.
        length: Bytes to return (max 1,000,000).
    """
    _s, v = _ctx()
    full = v.resolve(path)
    if not full.is_file():
        raise ToolError(f"no file at {path!r}")
    data = full.read_bytes()
    offset = max(0, min(offset, len(data)))
    end = min(len(data), offset + max(1, min(length, MAX_READ)))
    chunk = data[offset:end]
    content, encoding = None, "base64"
    # Back off up to 3 bytes so a chunk never splits a UTF-8 character.
    for trim in range(4 if end < len(data) else 1):
        try:
            content = chunk[: len(chunk) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
        end -= trim
        encoding = "utf-8"
        break
    if content is None:
        content = base64.b64encode(chunk).decode("ascii")
    out: dict[str, Any] = {
        "path": v.rel(full),
        "sha256": Vault.version(data),
        "size": len(data),
        "offset": offset,
        "length": end - offset,
        "encoding": encoding,
        "content": content,
    }
    if end < len(data):
        out["next_offset"] = end
    return out


def _stage_file(s: Settings, rel: str) -> Path:
    stage = s.audit_log.parent / "staged"
    stage.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _STAGE_HOURS * 3600
    for old in stage.glob("*.part"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return stage / f"{hashlib.sha256(rel.encode()).hexdigest()[:24]}.part"


@_locked
def write_file(
    path: str,
    content: str = "",
    encoding: str = "utf-8",
    if_absent: bool = False,
    if_version: str | None = None,
    more: bool = False,
    sha256: str | None = None,
    lock: str | None = None,
) -> dict[str, Any]:
    """Write a vault file byte for byte: no frontmatter changes, no added heading.

    For verbatim imports (e.g. full-text notes). Say how: ``if_absent=true``
    creates a new file and refuses to overwrite; ``if_version`` (the sha256
    from read_file or read_note) replaces a file only if it's unchanged.
    A big file goes in chunks: send each chunk with ``more=true`` (kept
    outside the vault, for 24 h), then the last with ``more=false``; only then
    is the file written, in one go. Max 5 MB.

    Args:
        path: Vault path, with its extension, e.g. "Notes/Full Text 21.md".
        content: The text (or this chunk of it).
        encoding: "utf-8" (default) or "base64" for binary content.
        if_absent: Create only; refuse if the file exists.
        if_version: Replace only if the file still has this sha256.
        more: true stages this chunk and waits for more; false writes.
        sha256: The whole file's expected sha256; refuses on a mismatch.
        lock: The id from begin_write, when a lock covers this file.
    """
    s, v = _ctx()
    rel = v.rel(v.resolve(path, write=True))
    _lock_check(s, rel)
    if not more and if_absent == bool(if_version):
        raise ToolError(
            "say how to write: if_absent=true for a new file, or if_version=<sha256 "
            "from read_file/read_note> to replace one"
        )
    if encoding not in ("utf-8", "base64"):
        raise ToolError("encoding is 'utf-8' or 'base64'")
    try:
        chunk = (
            base64.b64decode(content, validate=True)
            if encoding == "base64"
            else content.encode("utf-8")
        )
    except ValueError as exc:
        raise ToolError(f"content isn't valid base64: {exc}") from exc
    stage = _stage_file(s, rel)
    staged = stage.read_bytes() if stage.exists() else b""
    data = staged + chunk
    if len(data) > MAX_WRITE:
        raise ToolError(f"over {MAX_WRITE // 2**20} MB, Obsidian Sync's limit")
    if more:
        with stage.open("ab") as f:
            f.write(chunk)
        return {
            "path": rel,
            "staged_bytes": len(data),
            "next": "send the next chunk with more=true, or the last with more=false",
        }
    digest = Vault.version(data)
    if sha256 and sha256.lower() != digest:
        raise ToolError(
            f"the content's sha256 is {digest}, not {sha256}; nothing was written "
            "(staged chunks are kept: resend the last chunk)"
        )
    v.write_bytes(rel, data, expect_version=if_version or None)
    stage.unlink(missing_ok=True)
    _audit.record(
        s,
        "write_file",
        [rel],
        f"wrote {len(data)} bytes ({'replaced' if if_version else 'created'})",
        backend=FS,
        sha256=digest,
    )
    return {"backend": FS, **_receipt(rel, data), "sha256": digest}


# --- checks -------------------------------------------------------------------------


class Schema(TypedDict):
    required: NotRequired[list[str]]
    allowed: NotRequired[dict[str, list[Any]]]
    types: NotRequired[dict[str, str]]


_KINDS = ("text", "number", "boolean", "date", "datetime", "list", "object")


def validate(
    schema: Schema,
    folder: str | None = None,
    type: str | None = None,
    where: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Check notes against a property schema: required properties, allowed
    values and property types. Nothing is changed.

    Example: schema={"required": ["batch", "entries"], "allowed":
    {"question-status": ["open", "settled", "declined"]}, "types":
    {"entries": "number", "dlc": "boolean"}}, folder="Gaming/Elden Ring/Lore".

    Args:
        schema: {"required"?: [property], "allowed"?: {property: [values]},
            "types"?: {property: text|number|boolean|date|datetime|list|object}}.
            A list property passes ``allowed`` if every item is allowed.
        folder: Only notes under this folder.
        type: Only notes of this type.
        where: Only notes with these property values (as in bulk_update).
        limit: Maximum notes with problems to list.
    """
    _s, v = _ctx()
    bad = {k: t for k, t in schema.get("types", {}).items() if t not in _KINDS}
    if bad:
        raise ToolError(f"unknown types {bad}; use {', '.join(_KINDS)}")
    notes = _select(v, folder=folder, type=type)
    if where:
        notes = [n for n in notes if _where(n, where)]
    allowed = {
        k: {str(x) for x in vals} for k, vals in schema.get("allowed", {}).items()
    }
    problems: list[dict[str, Any]] = []
    tally: Counter[str] = Counter()
    for n in notes:
        found = []
        if not n.frontmatter_ok:
            found.append("frontmatter is not valid YAML")
        fm = n.frontmatter
        for k in schema.get("required", []):
            if fm.get(k) in (None, "", []):
                found.append(f"missing {k}")
                tally[f"missing {k}"] += 1
        for k, ok in allowed.items():
            if fm.get(k) in (None, "", []):
                continue
            out_of_set = [x for x in _notes.as_list(fm.get(k)) if x not in ok]
            if out_of_set:
                found.append(f"{k}: {out_of_set} not in {sorted(ok)}")
                tally[f"{k} not allowed"] += 1
        for k, want in schema.get("types", {}).items():
            have = n.kinds.get(k)
            if have and have != want:
                found.append(f"{k} is a {have}, not a {want}")
                tally[f"{k} not a {want}"] += 1
        if found:
            problems.append({"path": n.path, "problems": found})
    return {
        "backend": FS,
        "checked": len(notes),
        "ok": len(notes) - len(problems),
        "with_problems": len(problems),
        "summary": dict(tally.most_common()),
        "notes": problems[: max(1, min(limit, 1000))],
    }


def unresolved_links(folder: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Every link target in the vault (or a folder) that points at no note or
    file, with how many notes hold it and which.

    Args:
        folder: Only links in notes under this folder.
        limit: Maximum targets to list (most-linked first).
    """
    _s, v = _ctx()
    notes = v.notes()
    files = v.files()
    # Every path suffix a link can name: "a/b/c.md" -> "a/b/c.md", "b/c.md", "c.md".
    suffixes = set()
    for n in notes:
        parts = n.path.lower().split("/")
        suffixes.update("/".join(parts[i:]) for i in range(len(parts)))
    holders: dict[str, list[str]] = {}
    for n in notes:
        if folder and not n.path.startswith(folder.strip("/") + "/"):
            continue
        for t in n.links:
            key = t.strip().lower()
            key = key if key.endswith(".md") else f"{key}.md"
            if key in suffixes or v.attachment(t, files):
                continue
            holders.setdefault(t, []).append(n.path)
    ranked = sorted(holders.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    return {
        "backend": FS,
        "targets": len(ranked),
        "notes_with_unresolved": len({p for _, ps in ranked for p in ps}),
        "unresolved": [
            {"target": t, "count": len(ps), "notes": ps[:20]}
            for t, ps in ranked[: max(1, min(limit, 1000))]
        ],
    }


# --- Bases ----------------------------------------------------------------------------

_BASE_EMBED = re.compile(
    r"!\[\[([^\[\]|#]+?\.base)(?:#([^\[\]|]+))?(?:\|[^\[\]]*)?\]\]"
)


def _base_file(v: Vault, base: str) -> tuple[str, str | None]:
    """``(path of the .base file, view named by an embed)`` for a base or a note embedding one."""
    files = v.files()
    name = base.strip()
    if name.lower().endswith(".base") or f"{name}.base" in files:
        hit = v.attachment(name if name.endswith(".base") else f"{name}.base", files)
        if not hit:
            raise ToolError(f"no base file {name!r}")
        return hit, None
    text = v.read_text(_find(v, name).path)
    for target, view in _BASE_EMBED.findall(text):
        if hit := v.attachment(target, files):
            return hit, view or None
    if re.search(r"^(```|~~~)base\b", text, re.M):
        raise ToolError(
            "this note's base is an inline ```base block, which Obsidian's CLI can't "
            "query; move it into a .base file and embed it with ![[Name.base#View]]"
        )
    raise ToolError(f"{name!r} is neither a .base file nor a note that embeds one")


def _summary(kind: str, values: list[Any]) -> Any:
    filled = [x for x in values if x not in (None, "", [])]
    nums = [x for x in filled if isinstance(x, int | float) and not isinstance(x, bool)]
    k = kind.strip().lower()
    if k in ("sum", "average", "min", "max", "range", "median"):
        if not nums:
            return None
        nums.sort()
        return {
            "sum": sum(nums),
            "average": sum(nums) / len(nums),
            "min": nums[0],
            "max": nums[-1],
            "range": nums[-1] - nums[0],
            "median": nums[len(nums) // 2]
            if len(nums) % 2
            else (nums[len(nums) // 2 - 1] + nums[len(nums) // 2]) / 2,
        }[k]
    if k in ("earliest", "latest"):
        dates = sorted(str(x) for x in filled)
        return (dates[0] if k == "earliest" else dates[-1]) if dates else None
    return {
        "empty": len(values) - len(filled),
        "filled": len(filled),
        "unique": len({json.dumps(x, sort_keys=True, default=str) for x in filled}),
        "checked": sum(1 for x in values if x is True),
        "unchecked": sum(1 for x in values if x is False),
    }.get(k, f"unsupported summary {kind!r}")


def query_base(base: str, view: str | None = None, limit: int = 200) -> dict[str, Any]:
    """Evaluate a Base with Obsidian itself and return its rows and summaries.

    For live numbers (e.g. entries summed by class) instead of a hand-typed
    total. Summaries (Sum, Average, Min, Max, Range, Median, Earliest, Latest,
    Empty, Filled, Unique, Checked, Unchecked) are computed from the view's
    ``summaries`` over its rows, and per group when the view groups rows by a
    column it shows. Needs the Obsidian app running.

    Args:
        base: A .base file ("_Meta/Vault Overview.base") or a note that embeds
            one with ![[Name.base#View]].
        view: View name; default the embed's view, else the base's first view.
        limit: Maximum rows to return (max 1000); summaries use all rows.
    """
    s, v = _ctx()
    rel, embed_view = _base_file(v, base)
    view = view or embed_view
    try:
        rows = _cli.run_json(s, "base:query", path=rel, view=view)
    except _cli.CliError as exc:
        raise ToolError(f"Obsidian couldn't query {rel}: {exc}") from exc
    if not isinstance(rows, list):
        raise ToolError(f"unexpected base:query output for {rel}: {str(rows)[:200]}")
    try:
        spec = yaml.safe_load(v.read_text(rel)) or {}
    except yaml.YAMLError:
        spec = {}
    views = spec.get("views") or []
    vdef = next((x for x in views if x.get("name") == view), views[0] if views else {})
    names = {
        pid: (meta or {}).get("displayName")
        for pid, meta in (spec.get("properties") or {}).items()
    }

    def column(pid: str) -> str | None:
        keys = rows[0].keys() if rows else ()
        for cand in (
            names.get(pid),
            names.get(f"note.{pid}"),
            pid,
            pid.split(".", 1)[-1],
        ):
            if cand and cand in keys:
                return cand
        return None

    summaries: dict[str, Any] = {}
    group_pid = ((vdef.get("groupBy") or {}).get("property")) if vdef else None
    group_col = column(group_pid) if group_pid else None
    for pid, kind in ((vdef or {}).get("summaries") or {}).items():
        col = column(pid)
        if col is None:
            summaries[pid] = {"type": kind, "error": "not a column of this view"}
            continue
        entry: dict[str, Any] = {
            "type": kind,
            "value": _summary(str(kind), [r.get(col) for r in rows]),
        }
        if group_col:
            groups: dict[str, list[Any]] = {}
            for r in rows:
                groups.setdefault(str(r.get(group_col)), []).append(r.get(col))
            entry["by_group"] = {g: _summary(str(kind), xs) for g, xs in groups.items()}
        summaries[col] = entry
    limit = max(1, min(limit, 1000))
    out: dict[str, Any] = {
        "backend": CLI,
        "base": rel,
        "view": view or (vdef or {}).get("name"),
        "total": len(rows),
        "rows": rows[:limit],
    }
    if summaries:
        out["summaries"] = summaries
    if group_col:
        out["grouped_by"] = group_col
    if len(rows) > limit:
        out["rows_note"] = f"showing {limit} of {len(rows)} rows"
    return out


# --- scripts --------------------------------------------------------------------------

SCRIPTS_DIR = "_Meta/scripts"
_RUNNERS = {".py": ["python3", "-I"], ".sh": ["bash"]}
# Inside the front end's 25 s call, with the snapshot before it.
_SCRIPT_SECONDS = 20.0
_OUTPUT_FILES = 10


@_locked
def run_script(
    name: str, args: list[str] | None = None, lock: str | None = None
) -> dict[str, Any]:
    """Run an approved script from _Meta/scripts on the vault server.

    Only a script whose exact contents the owner approved (its sha256 pinned in
    the server's settings) runs; an edited script needs approving again. It
    runs as ``<script> <vault root> [args...]`` for up to ~20 s, after a git
    snapshot, with no network credentials. Returns its output, the notes it
    changed, and small files it left in its home directory.

    Args:
        name: Script file name, e.g. "elden-ring-characters.py".
        args: Extra arguments after the vault root.
        lock: The id from begin_write; refused while another session holds a lock.
    """
    s, v = _ctx()
    args = args or []
    if "/" in name or name.startswith(".") or not name.strip():
        raise ToolError("give a script's file name, e.g. 'elden-ring-characters.py'")
    if len(args) > 20 or any(len(a) > 500 for a in args):
        raise ToolError("at most 20 arguments of 500 characters")
    rel = f"{SCRIPTS_DIR}/{name}"
    path = v.resolve(rel)
    pins = dict(s.scripts)
    if not path.is_file():
        found = sorted(f.name for f in path.parent.glob("*") if f.is_file())
        raise ToolError(
            f"no script {name!r} in {SCRIPTS_DIR}; there: "
            + ", ".join(
                f"{f} ({'approved' if f in pins else 'not approved'})" for f in found
            )
        )
    runner = _RUNNERS.get(PurePosixPath(name).suffix)
    if runner is None:
        raise ToolError(f"only {', '.join(_RUNNERS)} scripts can run")
    code = path.read_bytes()
    digest = Vault.version(code)
    approve = (
        f"add {name}={digest} to the SSM parameter /obsidian-vault/scripts "
        "(comma-separated) and run deploy.sh push"
    )
    if name not in pins:
        raise ToolError(
            f"{name} isn't approved to run. To approve this version: {approve}"
        )
    if pins[name] != digest:
        raise ToolError(
            f"{name} changed since it was approved (approved {pins[name][:12]}, now "
            f"{digest[:12]}); review it, then {approve}"
        )
    others = [lk for lk in _locks.live(s) if lk.id != lock]
    if others:
        raise ToolError(
            f"a script may write anywhere, and {_locks.describe(others[0])}; wait for it"
        )
    started = time.monotonic()
    before = {n.path: (n.mtime, n.size) for n in v.notes()}
    out: dict[str, Any] = {"script": rel, "sha256": digest}
    if s.snapshot_cmd:
        out["snapshot"] = _snapshot(s, f"before run_script {name}")
    timeout = max(3.0, _SCRIPT_SECONDS - (time.monotonic() - started))
    with tempfile.TemporaryDirectory(prefix="vault-script-") as tmp:
        home = Path(tmp, "home")
        home.mkdir()
        script = Path(tmp, name)
        script.write_bytes(code)  # run exactly the bytes that were checked
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "TZ": s.timezone,
            "VAULT_ROOT": str(v.root),
        }
        try:
            proc = subprocess.run(  # noqa: S603 -- pinned script, argv list
                [*runner, str(script), str(v.root), *args],
                cwd=home,
                env=env,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            out["exit_code"] = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            out["exit_code"] = None
            out["error"] = f"stopped after {timeout:.0f} s; its writes so far are kept"
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
        out["stdout"] = stdout.decode("utf-8", "replace")[-20_000:]
        if stderr:
            out["stderr"] = stderr.decode("utf-8", "replace")[-5_000:]
        files = sorted(p for p in home.rglob("*") if p.is_file())[:_OUTPUT_FILES]
        outputs = {
            p.relative_to(home).as_posix(): p.read_text("utf-8", "replace")
            for p in files
            if p.stat().st_size <= 64_000
        }
        if outputs:
            out["outputs"] = outputs
    after = {n.path: (n.mtime, n.size) for n in v.notes()}
    changed = sorted(p for p, st in after.items() if before.get(p) != st)
    deleted = sorted(set(before) - set(after))
    out["changed"] = changed[:200]
    out["changed_count"] = len(changed)
    if deleted:
        out["deleted"] = deleted[:200]
    _audit.record(
        s,
        "run_script",
        changed + deleted,
        f"ran {name} {args} (exit {out['exit_code']})",
        backend=FS,
        sha256=digest,
    )
    return out


# --- locks and git ------------------------------------------------------------------


def begin_write(
    owner: str,
    reason: str,
    paths: list[str] | None = None,
    folders: list[str] | None = None,
    minutes: float = 60,
) -> dict[str, Any]:
    """Reserve notes or folders for a batch of writes by one session.

    While the lock lasts, writes to those paths from anyone else are refused,
    and writes naming the lock must stay inside it. Pass the returned ``lock``
    to every write tool, then call end_write to commit the work to git and
    release it. A lock expires on its own after ``minutes``.

    Args:
        owner: Who holds it, e.g. "Elden Ring batch 21 session".
        reason: What it's for; shown to anyone refused.
        paths: Notes or files (paths or titles), including ones about to be created.
        folders: Folders, recursive; "/" is the whole vault.
        minutes: How long it lasts (max 240).
    """
    s, v = _ctx()
    if not owner.strip() or not reason.strip():
        raise ToolError("give an owner and a reason")
    if not paths and not folders:
        raise ToolError("give paths and/or folders to lock")
    rels = []
    for item in paths or []:
        try:
            rels.append(v.find(item).path)
        except VaultError:
            rels.append(v.rel(v.resolve(item, write=True)))
    dirs = [
        "" if f.strip() in ("", "/") else v.rel(v.resolve(f.strip("/"), write=True))
        for f in folders or []
    ]
    try:
        lk = _locks.take(s, owner.strip(), rels, dirs, reason.strip(), minutes)
    except _locks.LockError as exc:
        raise ToolError(str(exc)) from exc
    _audit.record(
        s, "begin_write", [*rels, *dirs], f"lock {lk.id} by {owner}: {reason}"
    )
    return {
        **lk.public(),
        "lock": lk.id,
        "next": f"pass lock={lk.id!r} to each write; finish with end_write",
    }


def end_write(
    lock: str, owner: str, message: str, break_lock: bool = False
) -> dict[str, Any]:
    """Finish a lock: commit its paths to git as one commit, then release it.

    Args:
        lock: The id from begin_write.
        owner: The owner given to begin_write.
        message: The commit message, naming the batch or task.
        break_lock: Release another owner's lock without committing (logged);
            only for a lock whose session is gone.
    """
    s, _v = _ctx()
    try:
        lk = _locks.get(s, lock)
    except _locks.LockError as exc:
        raise ToolError(str(exc)) from exc
    if lk.owner != owner.strip():
        if not break_lock:
            raise ToolError(
                f"{_locks.describe(lk)}; pass break_lock=true only if that session is gone"
            )
        _locks.release(s, lk.id)
        _audit.record(
            s,
            "break_lock",
            [*lk.paths, *lk.folders],
            f"{owner} broke {_locks.describe(lk)}",
        )
        return {"released": lk.id, "broken": True, "commit": None}
    paths = None if "" in lk.folders else [*lk.paths, *lk.folders]
    try:
        commit = _git.commit(
            s, f"{message}\n\nLock {lk.id} by {lk.owner}: {lk.reason}", paths
        )
    except _git.GitError as exc:
        commit = {"error": str(exc)}
    _locks.release(s, lk.id)
    _audit.record(
        s, "end_write", [*lk.paths, *lk.folders], f"released {lk.id}: {message}"
    )
    return {"released": lk.id, "commit": commit}


def _git_path(v: Vault, path: str) -> str:
    try:
        return v.find(path).path
    except VaultError:
        return v.rel(v.resolve(path))


def _check_rev(rev: str) -> str:
    if not rev or rev.startswith("-") or any(c.isspace() for c in rev):
        raise ToolError(f"not a revision: {rev!r}")
    return rev


def git_status() -> dict[str, Any]:
    """What changed in the vault since the last git commit (snapshot), and any write locks.

    The repository is the server's snapshot repo: it's committed before bulk
    edits and idle stops, and by git_commit and end_write.
    """
    s, _v = _ctx()
    items = _git.status(s)
    return {
        "head": _git.head(s),
        "total": len(items),
        "changed": items[:500],
        "locks": [lk.public() for lk in _locks.live(s)],
    }


_DIFF_CHARS = 100_000


def git_diff(
    path: str | None = None, rev: str = "HEAD", to: str | None = None
) -> dict[str, Any]:
    """A unified diff: the vault now against a commit, or between two commits.

    Args:
        path: Only this note, file or folder.
        rev: The commit to compare from (default the last commit).
        to: A second commit; default the vault as it is now.
    """
    s, v = _ctx()
    revs = [_check_rev(rev), *([_check_rev(to)] if to else [])]
    spec = ["--", _git_path(v, path)] if path else []
    stat = _git.run(
        s, "diff", "--no-color", "--no-ext-diff", "--stat=120", *revs, *spec
    )
    diff = _git.run(s, "diff", "--no-color", "--no-ext-diff", *revs, *spec)
    out: dict[str, Any] = {"stat": stat[-5_000:], "diff": diff[:_DIFF_CHARS]}
    if len(diff) > _DIFF_CHARS:
        out["note"] = (
            f"diff cut at {_DIFF_CHARS} of {len(diff)} characters; narrow with path"
        )
    if not to:
        new = [i["path"] for i in _git.status(s) if i["status"] == "??"]
        if path:
            p = _git_path(v, path)
            new = [x for x in new if x == p or x.startswith(p + "/")]
        if new:
            out["new_files_not_in_diff"] = new[:200]
    return out


def git_log(path: str | None = None, n: int = 20) -> dict[str, Any]:
    """Recent commits, newest first, with the files each one changed.

    Args:
        path: Only commits touching this note, file or folder.
        n: How many commits (max 200).
    """
    s, v = _ctx()
    spec = ["--", _git_path(v, path)] if path else []
    raw = _git.run(
        s,
        "log",
        f"-n{max(1, min(n, 200))}",
        "--no-renames",
        "--name-only",
        "--format=%x1e%h%x1f%aI%x1f%an%x1f%B",
        *spec,
    )
    commits = []
    for rec in raw.split("\x1e")[1:]:
        meta, _, files = rec.partition("\x1f")
        date_, _, rest = files.partition("\x1f")
        author, _, rest = rest.partition("\x1f")
        message, _, names = rest.rpartition("\n\n")
        if not message:  # no file list (e.g. an empty commit)
            message, names = names, ""
        changed = [x for x in names.splitlines() if x.strip()]
        commits.append(
            {
                "commit": meta,
                "date": date_,
                "author": author,
                "message": message.strip(),
                "files": changed[:20],
                "file_count": len(changed),
            }
        )
    return {"commits": commits}


def git_show(path: str, rev: str = "HEAD") -> dict[str, Any]:
    """A note or file as it was at a commit (e.g. to restore a passage).

    Args:
        path: Vault path of the note or file.
        rev: The commit (from git_log), default the last one.
    """
    s, v = _ctx()
    rel = _git_path(v, path)
    data = _git.run_bytes(s, "show", f"{_check_rev(rev)}:{rel}")
    try:
        content, encoding = data[:MAX_READ].decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data[:MAX_READ]).decode("ascii"), "base64"
    out: dict[str, Any] = {
        "path": rel,
        "rev": rev,
        "sha256": Vault.version(data),
        "size": len(data),
        "encoding": encoding,
        "content": content,
    }
    if len(data) > MAX_READ:
        out["note"] = f"cut at {MAX_READ} of {len(data)} bytes"
    return out


def git_commit(message: str, paths: list[str] | None = None) -> dict[str, Any]:
    """Commit the vault's current state to git (the server's snapshot repo).

    Args:
        message: What the change was, naming the batch or task.
        paths: Only these notes, files or folders; default everything changed.
    """
    s, v = _ctx()
    rels = [_git_path(v, p) for p in paths] if paths else None
    out = _git.commit(s, message, rels)
    _audit.record(
        s, "git_commit", rels or ["."], f"commit {out.get('commit')}: {message}"
    )
    return out


# --- server ---------------------------------------------------------------------

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
# Fetches a URL from the internet.
_IMPORT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

READ_TOOLS = (
    search,
    read_note,
    list_notes,
    get_links,
    get_backlinks,
    list_tags,
    list_properties,
    tasks,
    health,
    read_file,
    validate,
    unresolved_links,
    query_base,
    git_status,
    git_diff,
    git_log,
    git_show,
)
WRITE_TOOLS = (
    (create_note, _WRITE),
    (append_to_note, _WRITE),
    (update_note, _EDIT),
    (move_note, _EDIT),
    (delete_note, _EDIT),
    (bulk_update, _EDIT),
    (create_notes, _WRITE),
    (update_notes, _EDIT),
    (move_notes, _EDIT),
    (import_archive, _IMPORT),
    (write_file, _EDIT),
    (begin_write, _WRITE),
    (end_write, _WRITE),
    (git_commit, _WRITE),
    (run_script, _EDIT),
)


INSTRUCTIONS = (
    "Trey's Obsidian vault. Folders are areas of life; every note has "
    "type/status properties (see list_properties). Find notes with search "
    "or list_notes, then read_note. Follow _Meta/Vault Guide.md when "
    "writing: wikilinks, shortest form; quick thoughts go in the daily "
    "note's Capture section. For many notes at once use create_notes, "
    "update_notes, bulk_update or move_notes, or import_archive for a zip/tar "
    "at a URL, rather than one call per note. Bulk changes return a plan "
    "first; show it before applying. When other sessions may write the same "
    "notes, pass read_note's version as if_version, or take a lock with "
    "begin_write and finish with end_write (which commits to git)."
)


def create_server(readonly: bool) -> MCPServer:
    """The MCP server; with ``readonly`` the write tools aren't registered at all."""
    server = MCPServer("obsidian-vault", instructions=INSTRUCTIONS)
    for fn in READ_TOOLS:
        server.add_tool(_as_tool(fn), annotations=_RO)
    # daily_note reads, and creates the note only when the server is writable.
    server.add_tool(
        _as_tool(daily_note, serialize=True), annotations=_RO if readonly else _WRITE
    )
    if not readonly:
        for fn, ann in WRITE_TOOLS:
            server.add_tool(_as_tool(fn, serialize=True), annotations=ann)
    return server


# Sync tools run on worker threads and claude.ai issues calls in parallel, so
# writes take turns: two appends to the daily note must not read the same text.
_WRITE_LOCK = threading.Lock()


def _as_tool(fn: Callable[..., Any], *, serialize: bool = False) -> Callable[..., Any]:
    """``fn`` with vault errors shown to the model, and writes serialized."""

    @functools.wraps(fn)
    def tool(*args: Any, **kwargs: Any) -> Any:
        s = get_settings()
        if fn is not health and s.require_ready:
            ready, st = _ready.check(s)
            if not ready and st is not None:
                raise ToolError(
                    "The vault server just started and Obsidian Sync is still catching "
                    f"up (sync status {st['sync_status']!r}, quiet {st['quiet_seconds']} s). "
                    "Wait about 30 seconds and call start_vault() or retry."
                )
        try:
            if serialize:
                with _WRITE_LOCK:
                    return fn(*args, **kwargs)
            return fn(*args, **kwargs)
        except (
            VaultError,
            _notes.FrontmatterError,
            _locks.LockError,
            _git.GitError,
        ) as exc:
            raise ToolError(str(exc)) from exc

    return tool
