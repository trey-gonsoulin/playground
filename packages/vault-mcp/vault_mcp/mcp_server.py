"""MCP tools for an Obsidian vault on an always-on server (brief §6.3).

The official Obsidian CLI is used for search, moves/renames, deletes, tasks and
creating today's daily note; everything else, and every CLI action whose app is
down, goes straight to the files. Each result names the ``backend`` it used.
Write tools follow ``_Meta/Vault Guide.md``: new notes land in ``_Inbox`` with
``type``/``status``/``created``, renames keep the old title in ``aliases``,
nothing is deleted permanently, and every write is audit-logged.
"""

import difflib
import functools
import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import cache
from pathlib import PurePosixPath
from typing import Any, NotRequired, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from vault_mcp import _archive, _audit, _cli, _notes, _ready
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


def read_note(note: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
    """Read a note: body (paged by line), frontmatter, outgoing links and backlinks.

    Args:
        note: Vault path ("Health/Health.md"), title ("Rug Options") or alias.
        offset: First body line to return (0-based), for paging long notes.
        limit: Number of body lines to return (max 500).
    """
    _s, v = _ctx()
    n = _find(v, note)
    fm, body, ok = _notes.split(v.read_text(n.path))
    lines = body.split("\n")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    end = min(len(lines), offset + limit)
    notes = v.notes()
    links, unresolved = [], []
    for t in n.links:
        hit = v.resolve_link(t, notes)
        (links if hit else unresolved).append(hit.path if hit else t)
    out: dict[str, Any] = {
        "backend": FS,
        "path": n.path,
        "title": n.title,
        "frontmatter": fm if ok else {"error": "frontmatter is not valid YAML"},
        "content": "\n".join(lines[offset:end]),
        "lines": f"{offset + 1}-{end} of {len(lines)}",
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
    out = []
    for t in n.links:
        hit = v.resolve_link(t, notes)
        out.append({"target": t, "path": hit.path if hit else None})
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


def create_note(
    title: str,
    body: str = "",
    folder: str = "_Inbox",
    type: str = "note",
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a note following the Vault Guide.

    New notes go to ``_Inbox`` unless a folder is given, and get ``type``,
    ``status: active``, ``created`` (today) and ``tags: []``; a note in a
    top-level area folder gets ``up`` pointing at that folder's overview note.
    Link people and notes with wikilinks (``[[Person Name]]``).

    Args:
        title: Note title (the filename); say what the note is, dates first
            for dated entries ("2026-11-11 Workout").
        body: Markdown body. An "# <title>" heading is added if it has none.
        folder: Vault folder, e.g. "Home" or "TTRPG/Murder on the Eberron Express/NPCs".
        type: One of the Vault Guide types (note, list, recipe, journal, ...).
        properties: Extra frontmatter, e.g. {"people": ["[[Person Name]]"],
            "up": "[[Murder on the Eberron Express]]", "tags": ["eberron"]}.
    """
    s, v = _ctx()
    rel, text, fm, warnings = _new_note(s, v, title, body, folder, type, properties)
    try:
        v.write_text(rel, text, create=True)
    except VaultError as exc:
        raise ToolError(str(exc)) from exc
    _audit.record(s, "create_note", [rel], f"created ({type})", backend=FS)
    out: dict[str, Any] = {"backend": FS, "path": rel, "frontmatter": fm}
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
    warnings = _check_vocab(fm)
    if not body.lstrip().startswith("# "):
        body = f"# {title}\n\n{body}" if body.strip() else f"# {title}\n"
    if not body.endswith("\n"):
        body += "\n"
    return rel, _notes.join(fm, body), fm, warnings


class Patch(TypedDict):
    find: str
    replace: str
    replace_all: NotRequired[bool]


def update_note(
    note: str,
    body: str | None = None,
    patch: list[Patch] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a note: replace its body, apply find/replace patches, and/or merge properties.

    The frontmatter is only changed through ``properties``, so body edits can't
    break it. ``updated`` is not touched (it records an import's last edit).

    Args:
        note: Vault path, title or alias.
        body: New body text (everything below the frontmatter).
        patch: Find/replace edits on the body, applied in order. Each ``find``
            must occur exactly once unless ``replace_all`` is true.
        properties: Frontmatter keys to set; a null value removes the key.
            Lists replace whole lists, e.g. {"aliases": ["Old", "Older"]}.
    """
    s, v = _ctx()
    n, text, new, warnings, what = _edit_note(v, note, body, patch, properties)
    changed = new != text
    if changed:
        v.write_text(n.path, new, expect=text)
        _audit.record(
            s, "update_note", [n.path], f"updated {', '.join(what)}", backend=FS
        )
    out: dict[str, Any] = {"backend": FS, "path": n.path, "changed": changed}
    if warnings:
        out["warnings"] = warnings
    return out


def _edit_note(
    v: Vault,
    note: str,
    body: str | None = None,
    patch: list[Patch] | None = None,
    properties: dict[str, Any] | None = None,
) -> tuple[Note, str, str, list[str], list[str]]:
    """``(note, old text, new text, warnings, what changed)``, not written."""
    if body is None and not patch and not properties:
        raise ToolError("give body, patch or properties")
    n = _find(v, note)
    text = v.read_text(n.path)
    head, cur = _notes.split_raw(text)
    if body is not None:
        cur = body if body.endswith("\n") else body + "\n"
    for i, p in enumerate(patch or []):
        if not p["find"]:
            raise ToolError(f"patch {i}: 'find' is empty")
        hits = cur.count(p["find"])
        if hits == 0 or (hits > 1 and not p.get("replace_all")):
            raise ToolError(
                f"patch {i}: 'find' occurs {hits} times; it must occur exactly once "
                "(or set replace_all)"
            )
        cur = cur.replace(p["find"], p["replace"])
    new = head + cur
    warnings: list[str] = []
    if properties:
        warnings = _check_vocab(properties)
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


def append_to_note(note: str, text: str, heading: str | None = None) -> dict[str, Any]:
    """Append text to the end of a note, or to the end of one heading's section.

    For today's daily note use ``daily_note`` first (it creates the note), then
    e.g. append_to_note(path, "- buy cat litter", heading="Capture"). An empty
    template bullet ("- ") at the end of the section is replaced.

    Args:
        note: Vault path, title or alias.
        text: Markdown to add; include list markers ("- ", "- [ ] ") yourself.
        heading: Heading text (without #s) whose section to append to.
    """
    s, v = _ctx()
    n = _find(v, note)
    raw = v.read_text(n.path)
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
    return {"backend": FS, "path": n.path, "appended": text, "heading": heading}


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


def move_note(
    note: str, new_path_or_title: str, dry_run: bool | None = None
) -> dict[str, Any]:
    """Rename or move a note, updating every link to it, and keep the old title as an alias.

    Args:
        note: Vault path, title or alias of the note to move.
        new_path_or_title: A new title ("Living Room Rug Options", same folder),
            a folder ending in "/" ("Home/"), or a full path ("Home/Rugs/Rug Options").
        dry_run: true returns the plan only; false applies it. By default it
            applies unless more than ~10 notes would change, in which case it
            returns the plan and you call again with dry_run=false.
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


def delete_note(note: str) -> dict[str, Any]:
    """Delete a note to Obsidian's trash (never asks for a permanent delete).

    Obsidian follows the vault's "Deleted files" setting, which should be
    ".trash folder" (see README). If Obsidian is down the note is moved to
    ``_Review/To delete/`` instead.

    Args:
        note: Vault path, title or alias.
    """
    s, v = _ctx()
    n = _find(v, note)
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


def bulk_update_properties(
    set_properties: dict[str, Any],
    folder: str | None = None,
    type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    person: str | None = None,
    updated_before: str | None = None,
    updated_after: str | None = None,
    paths: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Set (or, with null, remove) properties on every note matching a filter.

    Always returns a plan first (dry_run=true). Show it to the user, then call
    again with the same arguments and dry_run=false to apply.
    Example: archive lists not updated since 2023 ->
    set_properties={"status": "archive"}, type="list", status="active",
    updated_before="2023-01-01".

    Args:
        set_properties: Frontmatter keys to set; null removes the key.
        folder, type, status, tag, person, updated_before, updated_after:
            Filters, as in list_notes.
        paths: Explicit notes (paths or titles) instead of, or as well as, filters.
        dry_run: true (default) returns the plan; false applies it.
    """
    s, v = _ctx()
    if not set_properties:
        raise ToolError("set_properties is empty")
    filters = {
        "folder": folder,
        "type": type,
        "status": status,
        "tag": tag,
        "person": person,
        "updated_before": updated_before,
        "updated_after": updated_after,
    }
    if not paths and not any(filters.values()):
        raise ToolError(
            "give at least one filter or paths; refusing to touch every note"
        )
    warnings = _check_vocab(set_properties)
    targets = _select(v, **filters) if any(filters.values()) else []
    if paths:
        explicit = {_find(v, p).path for p in paths}
        targets = (
            [t for t in targets if t.path in explicit]
            if targets
            else [_find(v, p) for p in sorted(explicit)]
        )
    plan, skipped = [], []
    for n in targets:
        if not n.frontmatter_ok:
            skipped.append(n.path)
            continue
        changes = {
            k: [n.frontmatter.get(k), val]
            for k, val in set_properties.items()
            if n.frontmatter.get(k) != val
        }
        if changes:
            plan.append({"path": n.path, "changes": changes})
    out: dict[str, Any] = {
        "dry_run": dry_run,
        "matched": len(targets),
        "to_change": len(plan),
        "plan": plan[:100],
    }
    if skipped:
        out["skipped_invalid_frontmatter"] = skipped
    if warnings:
        out["warnings"] = warnings
    if dry_run:
        out["apply_with"] = "the same arguments and dry_run=false"
        return out
    if plan and s.snapshot_cmd:
        out["snapshot"] = _snapshot(
            s, f"before bulk_update_properties on {len(plan)} notes"
        )
    for item in plan:
        rel = item["path"]
        v.write_text(rel, _notes.set_properties(v.read_text(rel), set_properties))
    _audit.record(
        s,
        "bulk_update_properties",
        [p["path"] for p in plan],
        f"set {set_properties} on {len(plan)} notes",
        backend=FS,
    )
    out["backend"] = FS
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


class NoteEdit(TypedDict):
    note: str
    body: NotRequired[str]
    patch: NotRequired[list[Patch]]
    properties: NotRequired[dict[str, Any]]


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


def create_notes(notes: list[NewNote], dry_run: bool | None = None) -> dict[str, Any]:
    """Create many notes in one call, each following the Vault Guide as in create_note.

    Every note is checked first (title, folder, vocabulary, no existing file,
    no duplicate in the batch); if any fails, nothing is written and the errors
    come back. For files that already exist somewhere as a zip or tar, use
    import_archive instead.

    Args:
        notes: Up to 200 notes, each {"title", "body"?, "folder"? (default
            "_Inbox"), "type"? (default "note"), "properties"?}.
        dry_run: true returns the plan only; false creates. By default it
            creates up to ~10 notes and returns the plan for more; then call
            again with the same notes and dry_run=false.
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
    try:
        for rel, text in planned:
            v.write_text(rel, text, create=True)
            created.append(rel)
    except VaultError as exc:
        raise ToolError(
            f"stopped after creating {len(created)} of {len(planned)} notes: {exc}"
        ) from exc
    finally:
        if created:
            _audit.record(
                s, "create_notes", created, f"created {len(created)} notes", backend=FS
            )
    out.update(dry_run=False, backend=FS)
    return out


def update_notes(updates: list[NoteEdit], dry_run: bool = True) -> dict[str, Any]:
    """Edit many notes in one call: each gets a new body, find/replace patches
    and/or properties, exactly as in update_note.

    Always returns a plan first (dry_run=true) with a short diff per note. Show
    it to the user, then call again with the same updates and dry_run=false.
    Nothing is written if any update fails its checks. To set the same
    properties on many notes, bulk_update_properties is simpler.

    Args:
        updates: Up to 200 edits, each {"note", "body"?, "patch"?, "properties"?}
            with the meanings of update_note's arguments; one edit per note.
        dry_run: true (default) returns the plan; false applies it.
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
                v, u["note"], u.get("body"), u.get("patch"), u.get("properties")
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
    try:
        for path, old, new in edits:
            v.write_text(path, new, expect=old)
            written.append(path)
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
    out["backend"] = FS
    return out


def move_notes(moves: list[Move], dry_run: bool = True) -> dict[str, Any]:
    """Rename or move many notes, each as in move_note: links to it are updated
    and the old title is kept as an alias.

    Always returns a plan first; show it, then call again with the same moves
    and dry_run=false. Applying runs for at most ~15 s; anything not reached
    comes back as ``remaining``: call move_notes(moves=remaining, dry_run=false).

    Args:
        moves: Up to 200 {"note", "to"}; ``note`` is a path, title or alias and
            ``to`` is a new title, a folder ending in "/" or a full path.
        dry_run: true (default) returns the plan; false applies it.
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


def import_archive(
    url: str,
    folder: str = "_Inbox",
    add_properties: bool = True,
    strip_top_folder: bool = True,
    sha256: str | None = None,
    dry_run: bool = True,
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
)
WRITE_TOOLS = (
    (create_note, _WRITE),
    (append_to_note, _WRITE),
    (update_note, _EDIT),
    (move_note, _EDIT),
    (delete_note, _EDIT),
    (bulk_update_properties, _EDIT),
    (create_notes, _WRITE),
    (update_notes, _EDIT),
    (move_notes, _EDIT),
    (import_archive, _IMPORT),
)


INSTRUCTIONS = (
    "Trey's Obsidian vault. Folders are areas of life; every note has "
    "type/status properties (see list_properties). Find notes with search "
    "or list_notes, then read_note. Follow _Meta/Vault Guide.md when "
    "writing: wikilinks, shortest form; quick thoughts go in the daily "
    "note's Capture section. For many notes at once use create_notes, "
    "update_notes or move_notes, or import_archive for a zip/tar at a URL, "
    "rather than one call per note. Bulk changes return a plan first; show "
    "it before applying."
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
        except (VaultError, _notes.FrontmatterError) as exc:
            raise ToolError(str(exc)) from exc

    return tool
