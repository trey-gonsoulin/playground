"""Filesystem view of the vault: safe path resolution, a note index and link graph.

Every path a tool receives goes through ``Vault.resolve``, which keeps it inside
the vault root (no ``..``, absolute paths or symlinks out) and, for writes, out
of ``.obsidian``, ``.trash`` and ``.git``. The index re-parses only notes whose
mtime or size changed, so a full scan of ~600 notes per call is cheap.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from vault_mcp import _notes
from vault_mcp.config import Settings

_HIDDEN_DIRS = {".obsidian", ".trash", ".git"}
_BAD_TITLE_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


class VaultError(ValueError):
    """A request the vault can't satisfy (bad path, missing or ambiguous note)."""


@dataclass
class Note:
    path: str  # vault-relative, POSIX, with .md
    mtime: float
    size: int
    frontmatter: dict[str, Any]
    frontmatter_ok: bool
    links: list[str]  # raw wikilink targets, body + frontmatter
    tags: list[str]

    @property
    def title(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def folder(self) -> str:
        parent = str(PurePosixPath(self.path).parent)
        return "" if parent == "." else parent

    @property
    def aliases(self) -> list[str]:
        return _notes.as_list(self.frontmatter.get("aliases"))

    def summary(self) -> dict[str, Any]:
        fm = self.frontmatter
        out: dict[str, Any] = {"path": self.path, "title": self.title}
        for key in ("type", "status", "created", "updated"):
            if fm.get(key) not in (None, ""):
                out[key] = fm[key]
        if self.tags:
            out["tags"] = self.tags
        return out


@dataclass
class Vault:
    settings: Settings
    _cache: dict[str, Note] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.settings.vault_root

    # --- paths ------------------------------------------------------------------

    def resolve(self, rel: str, *, write: bool = False) -> Path:
        """Absolute path for vault-relative ``rel``, refusing anything outside."""
        rel = rel.strip().replace("\\", "/")
        if not rel:
            raise VaultError("empty path")
        pure = PurePosixPath(rel)
        if pure.is_absolute() or rel.startswith("~"):
            raise VaultError(f"path must be relative to the vault: {rel!r}")
        if ".." in pure.parts:
            raise VaultError(f"'..' is not allowed in paths: {rel!r}")
        if write and _HIDDEN_DIRS & set(pure.parts):
            raise VaultError(f"writing inside {'/'.join(_HIDDEN_DIRS)} is not allowed")
        root = self.root.resolve()
        full = (root / pure).resolve()
        if full != root and root not in full.parents:
            raise VaultError(f"path leaves the vault (symlink?): {rel!r}")
        return full

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    @staticmethod
    def note_path(rel: str) -> str:
        rel = rel.strip().strip("/")
        return rel if rel.lower().endswith(".md") else f"{rel}.md"

    @staticmethod
    def clean_title(title: str) -> str:
        cleaned = _BAD_TITLE_CHARS.sub("-", title).strip().strip(".")
        if not cleaned:
            raise VaultError(f"unusable note title: {title!r}")
        return cleaned

    # --- index ------------------------------------------------------------------

    def _md_files(self) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in _HIDDEN_DIRS)
            out.extend(Path(dirpath, f) for f in sorted(filenames) if f.endswith(".md"))
        # os.walk doesn't enter symlinked dirs, but it does list symlinked files.
        root = self.root.resolve()
        return [p for p in out if not p.is_symlink() or root in p.resolve().parents]

    def notes(self) -> list[Note]:
        seen: dict[str, Note] = {}
        for path in self._md_files():
            st = path.stat()
            rel = path.relative_to(self.root).as_posix()
            cached = self._cache.get(rel)
            if cached is None or (cached.mtime, cached.size) != (
                st.st_mtime,
                st.st_size,
            ):
                cached = self._parse(rel, path.read_text("utf-8", "replace"), st)
            seen[rel] = cached
        self._cache = seen
        return list(seen.values())

    @staticmethod
    def _parse(rel: str, text: str, st: os.stat_result) -> Note:
        fm, body, ok = _notes.split(text)
        links = _notes.wikilinks(body)
        links += [t for t in _notes.frontmatter_links(fm) if t not in links]
        return Note(rel, st.st_mtime, st.st_size, fm, ok, links, _notes.tags(fm, body))

    def find(self, path_or_title: str) -> Note:
        """A note by vault path, title or alias (case-insensitive)."""
        notes = self.notes()
        want = path_or_title.strip().removeprefix("[[").removesuffix("]]")
        want = want.split("|")[0].split("#")[0].strip()
        by_path = {n.path.lower(): n for n in notes}
        hit = by_path.get(self.note_path(want).lower())
        if hit:
            return hit
        key = want.removesuffix(".md").lower()
        for label, matches in (
            ("title", [n for n in notes if n.title.lower() == key]),
            ("alias", [n for n in notes if key in {a.lower() for a in n.aliases}]),
            (
                "path suffix",
                [n for n in notes if n.path.lower().endswith(f"/{key}.md")],
            ),
        ):
            if len(matches) == 1:
                return matches[0]
            if matches:
                paths = ", ".join(n.path for n in matches[:10])
                raise VaultError(f"{want!r} matches several notes by {label}: {paths}")
        raise VaultError(f"no note found for {want!r}")

    def resolve_link(self, target: str, notes: list[Note] | None = None) -> Note | None:
        """The note a wikilink target points at, Obsidian-style, or None."""
        notes = self.notes() if notes is None else notes
        t = target.strip().lower()
        t_md = t if t.endswith(".md") else f"{t}.md"
        exact = [n for n in notes if n.path.lower() == t_md]
        if exact:
            return exact[0]
        suffix = [n for n in notes if n.path.lower().endswith(f"/{t_md}")]
        if suffix:
            # Obsidian prefers the shortest path when a bare title is ambiguous.
            return min(suffix, key=lambda n: (n.path.count("/"), n.path))
        return None

    def link_text(self, note_path: str, notes: list[Note] | None = None) -> str:
        """Shortest unambiguous wikilink target for ``note_path``."""
        notes = self.notes() if notes is None else notes
        stem = PurePosixPath(note_path).stem
        others = [
            n for n in notes if n.title.lower() == stem.lower() and n.path != note_path
        ]
        return note_path.removesuffix(".md") if others else stem

    def backlinks(self, note: Note) -> list[Note]:
        notes = self.notes()
        out = []
        for n in notes:
            if n.path == note.path:
                continue
            if any(
                (hit := self.resolve_link(t, notes)) is not None
                and hit.path == note.path
                for t in n.links
            ):
                out.append(n)
        return out

    # --- files ------------------------------------------------------------------

    def read_text(self, rel: str) -> str:
        return self.resolve(rel).read_text("utf-8")

    def write_text(
        self, rel: str, text: str, *, create: bool = False, expect: str | None = None
    ) -> None:
        """Atomically write ``rel``; with ``create``, refuse to overwrite.

        ``expect`` is the text the edit was based on: if the file changed since
        (Obsidian Sync landed an edit from another device), nothing is written.
        """
        self._check_writable()
        path = self.resolve(rel, write=True)
        if create and path.exists():
            raise VaultError(f"{rel} already exists")
        if not create and not path.is_file():
            raise VaultError(f"{rel} does not exist")
        if expect is not None and path.read_text("utf-8") != expect:
            raise VaultError(
                f"{rel} changed while it was being edited; read it again and retry"
            )
        self._atomic_write(path, text.encode("utf-8"))

    def write_bytes(self, rel: str, data: bytes) -> None:
        """Atomically create ``rel`` (an imported attachment); refuse to overwrite."""
        self._check_writable()
        path = self.resolve(rel, write=True)
        if path.exists():
            raise VaultError(f"{rel} already exists")
        self._atomic_write(path, data)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Dot-prefixed temp file: Obsidian and Sync ignore hidden files.
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=".vault-mcp-", suffix=".tmp"
        )
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _check_writable(self) -> None:
        # Belt and braces: in read-only mode the write tools aren't even registered.
        if self.settings.readonly:
            raise VaultError("the vault server is read-only (VAULT_MCP_READONLY)")

    def move_file(self, src: str, dst: str) -> None:
        self._check_writable()
        s, d = self.resolve(src, write=True), self.resolve(dst, write=True)
        if d.exists():
            raise VaultError(f"{dst} already exists")
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def obsidian_config(self, name: str) -> dict[str, Any]:
        """A JSON file from ``.obsidian/`` (e.g. ``daily-notes.json``), or {}."""
        try:
            return json.loads((self.root / ".obsidian" / name).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
