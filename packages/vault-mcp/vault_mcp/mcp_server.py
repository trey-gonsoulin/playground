"""MCP tools for an Obsidian vault on an always-on server (brief §6.3).

The official Obsidian CLI is used for search, moves/renames, deletes, tasks and
creating today's daily note; everything else, and every CLI action whose app is
down, goes straight to the files. Each result names the ``backend`` it used.
Write tools follow ``_Meta/Vault Guide.md``: new notes land in ``_Inbox`` with
``type``/``status``/``created``, renames keep the old title in ``aliases``,
nothing is deleted permanently, and every write is audit-logged.
"""

import functools
import json
import re
import shlex
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from functools import cache
from pathlib import PurePosixPath
from typing import Any, NotRequired, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from vault_mcp import _audit, _cli, _notes, _ready
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
        out.append(
            {
                "path": path,
                "line": item.get("line"),
                "status": item.get("status", "x" if item.get("completed") else " "),
                "text": text.strip(),
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
    try:
        title = v.clean_title(title)
        folder = folder.strip().rstrip("/")
        rel = f"{folder}/{title}.md" if folder else f"{title}.md"
        v.resolve(rel, write=True)
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
    try:
        v.write_text(rel, _notes.join(fm, body), create=True)
    except VaultError as exc:
        raise ToolError(str(exc)) from exc
    _audit.record(s, "create_note", [rel], f"created ({type})", backend=FS)
    out: dict[str, Any] = {"backend": FS, "path": rel, "frontmatter": fm}
    if warnings:
        out["warnings"] = warnings
    return out


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
    changed = new != text
    if changed:
        v.write_text(n.path, new, expect=text)
        what = [
            k
            for k, x in (("body", body), ("patch", patch), ("properties", properties))
            if x
        ]
        _audit.record(
            s, "update_note", [n.path], f"updated {', '.join(what)}", backend=FS
        )
    out: dict[str, Any] = {"backend": FS, "path": n.path, "changed": changed}
    if warnings:
        out["warnings"] = warnings
    return out


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
    notes = v.notes()
    linkers = v.backlinks(n)
    new_title = PurePosixPath(new_rel).stem
    alias = n.title if new_title != n.title and n.title not in n.aliases else None
    plan: dict[str, Any] = {
        "from": n.path,
        "to": new_rel,
        "alias_added": alias,
        "notes_with_links_to_update": [b.path for b in linkers],
    }
    if dry_run or (dry_run is None and len(linkers) + 1 > s.bulk_threshold):
        return {"dry_run": True, **plan, "apply_with": "dry_run=false"}
    if alias:
        v.write_text(
            n.path,
            _notes.set_properties(
                v.read_text(n.path), {"aliases": [*n.aliases, alias]}
            ),
        )
    backend, note_txt = CLI, None
    try:
        if PurePosixPath(new_rel).parent == PurePosixPath(n.path).parent:
            _cli.run(s, "rename", path=n.path, name=new_title)
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
    out: dict[str, Any] = {"backend": backend, "dry_run": False, **plan}
    if note_txt:
        out["note"] = note_txt
    return out


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


# --- server ---------------------------------------------------------------------

_RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

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
)


INSTRUCTIONS = (
    "Trey's Obsidian vault. Folders are areas of life; every note has "
    "type/status properties (see list_properties). Find notes with search "
    "or list_notes, then read_note. Follow _Meta/Vault Guide.md when "
    "writing: wikilinks, shortest form; quick thoughts go in the daily "
    "note's Capture section. Bulk changes return a plan first; show it "
    "before applying."
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
        if fn is not health and s.require_ready and not _ready.is_ready(s):
            st = _ready.state(s)
            raise ToolError(
                "The vault server just started and Obsidian Sync is still catching up "
                f"(sync status {st['sync_status']!r}, quiet {st['quiet_seconds']} s). "
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
