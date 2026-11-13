"""Markdown note parsing: YAML frontmatter, wikilinks, tags and template rendering.

Frontmatter round-trips as plain strings: dates stay ``"2026-11-10"`` instead of
becoming ``datetime.date``, so they serialize to JSON and are written back as-is.
``typed`` reads it the way Obsidian does instead (numbers, booleans, lists), for
showing values and checking property types.
"""

import re
from datetime import date, datetime
from typing import Any

import yaml

# Property vocabulary from _Meta/Vault Guide.md.
TYPES = (
    "note list recipe journal poem message reference document log campaign "
    "writing person index meta"
).split()
STATUSES = ("active", "archive", "draft")
SOURCES = ("apple-notes", "pc", "icloud-drive", "google-docs")
THEME_TAGS = (
    "daily dnd eberron eberron-express elden-ring irish astrology tarot".split()
)


class FrontmatterError(ValueError):
    """The note's frontmatter isn't valid YAML, so it can't be safely rewritten."""


# Plain scalars Obsidian (YAML 1.2 core schema) reads as something other than text.
_INT = re.compile(r"^[-+]?[0-9]+$")
_FLOAT = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")


class _Dumper(yaml.SafeDumper):
    """Writes new values the way Obsidian does: ``up:`` for empty, ``"[[Link]]"``.

    Numbers and booleans are written plain; text that Obsidian would read as a
    number, boolean or null (``"10.10"``, ``"true"``) is quoted, so it stays
    text. Dates stay plain, which Obsidian reads as a date.
    """

    yaml_implicit_resolvers: dict = {}  # noqa: RUF012 -- replaces the class attribute

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        # Indent block lists under their key, as Obsidian's properties editor does.
        super().increase_indent(flow, False)


def _repr_text(dumper: yaml.SafeDumper, value: Any) -> yaml.Node:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    quote = text.startswith("[[") or (
        isinstance(value, str)
        and text != ""
        and any(r.match(text) for r in (_INT, _FLOAT, _BOOL, _NULL))
    )
    style = '"' if quote else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


for _t in (str, type(None), bool, int, float):
    _Dumper.add_representer(_t, _repr_text)


class _TypedLoader(yaml.SafeLoader):
    """YAML 1.2 core scalars, as Obsidian reads them; dates stay text."""

    yaml_implicit_resolvers: dict = {}  # noqa: RUF012 -- replaces the class attribute


for _tag, _re, _first in (
    ("bool", _BOOL, "tTfF"),
    ("int", _INT, "-+0123456789"),
    ("float", _FLOAT, "-+.0123456789"),
    ("null", _NULL, ["~", "n", "N", ""]),
):
    _TypedLoader.add_implicit_resolver(f"tag:yaml.org,2002:{_tag}", _re, list(_first))
for _tag, _make in (
    ("bool", lambda x: x.lower() == "true"),
    ("int", int),
    ("float", float),
    ("null", lambda _x: None),
):
    _TypedLoader.add_constructor(
        f"tag:yaml.org,2002:{_tag}",
        lambda loader, node, make=_make: make(loader.construct_scalar(node)),
    )

BOM = "\ufeff"
_FM_RE = re.compile(
    "\\A\ufeff?" r"---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.S | re.M
)
# A top-level ``key:`` line in a frontmatter block.
_KEY_LINE = re.compile(
    r"""^(?:"([^"]+)"|'([^']+)'|([^\s#'"][^:]*?))[ \t]*:(?:[ \t]|\r?$)"""
)
# [[target]], [[target|alias]], [[target#heading]], [[target#^block]], ![[embed]],
# and [[target\|alias]] (the pipe escaped, as inside a table).
_LINK_RE = re.compile(
    r"!?\[\[((?:[^\[\]|#^\\]|\\(?![|#^]))*)(?:\\?[#^][^\[\]|]*)?(?:\\?\|[^\[\]]*)?\]\]"
)
_TAG_RE = re.compile(r"(?<![\w/#&`])#([A-Za-z][\w/-]*)")
_FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
# Wikilinks, markdown link targets and inline code, where a # isn't a tag.
_NOT_TAGS_RE = re.compile(r"\[\[[^\[\]\n]*\]\]|\]\([^)\n]*\)|`[^`\n]*`")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")


def split(text: str) -> tuple[dict[str, Any], str, bool]:
    """``(frontmatter, body, ok)``; ``ok`` is False unless it's a YAML mapping.

    Scalars stay text exactly as written (``"2026-11-10"``, ``"12:30"``, ``"yes"``).
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text.removeprefix(BOM), True
    try:
        data = yaml.load(m.group(1), Loader=yaml.BaseLoader)  # noqa: S506 -- strings only
    except yaml.YAMLError:
        return {}, text[m.end() :], False
    if data is None:
        return {}, text[m.end() :], True
    # A list or a paragraph between two thematic breaks isn't frontmatter we may rewrite.
    return (
        (data, text[m.end() :], True) if isinstance(data, dict) else ({}, text, False)
    )


def typed(text: str) -> dict[str, Any]:
    """The frontmatter as Obsidian reads it: numbers, booleans and lists typed, dates as text."""
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.load(m.group(1), Loader=_TypedLoader)  # noqa: S506 -- safe loader subclass
    except yaml.YAMLError:
        return {}
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def kind(value: Any) -> str | None:
    """A property value's Obsidian type (text, number, boolean, date, datetime,
    list, object), or None when empty."""
    if value is None or value in ("", []):
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    text = str(value)
    if _DATE.match(text):
        return "date"
    return "datetime" if _DATETIME.match(text) else "text"


def split_raw(text: str) -> tuple[str, str]:
    """``(frontmatter block verbatim, body)``, for body edits that keep it byte-exact."""
    m = _FM_RE.match(text)
    return (text[: m.end()], text[m.end() :]) if m else ("", text)


def _dump(data: dict[str, Any]) -> str:
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    )


def join(frontmatter: dict[str, Any], body: str) -> str:
    """A new note's text. Edit existing notes with ``set_properties``."""
    return f"---\n{_dump(frontmatter)}---\n{body}" if frontmatter else body


def set_properties(text: str, changes: dict[str, Any]) -> str:
    """Set (or, with ``None``, remove) top-level properties.

    Only the changed keys' lines are rewritten; every other line of the block
    stays byte-for-byte, so values are never reinterpreted.
    """
    _fm, _body, ok = split(text)
    if not ok:
        raise FrontmatterError(
            "frontmatter is not a valid YAML mapping; fix it by hand first"
        )
    m = _FM_RE.match(text)
    if m is None:
        bom = BOM if text.startswith(BOM) else ""
        new = {k: v for k, v in changes.items() if v is not None}
        return bom + join(new, text.removeprefix(bom)) if new else text
    # Split the block into segments, one per top-level key (with its continuation lines).
    segments: list[tuple[str | None, list[str]]] = []
    for line in m.group(1).splitlines(keepends=True):
        km = _KEY_LINE.match(line)
        if km:
            segments.append(
                (next(g for g in km.groups() if g is not None).strip(), [line])
            )
        elif segments:
            segments[-1][1].append(line)
        else:
            segments.append((None, [line]))
    for key, value in changes.items():
        idx = next((i for i, (k, _) in enumerate(segments) if k == key), None)
        if value is None:
            if idx is not None:
                del segments[idx]
            continue
        dumped = _dump({key: value}).splitlines(keepends=True)
        if idx is None:
            segments.append((key, dumped))
        else:
            segments[idx] = (key, dumped)
    block = "".join(line for _, lines in segments for line in lines)
    if block and not block.endswith("\n"):
        block += "\n"
    opening = text[: m.start(1)]
    closing = text[m.end(1) : m.end()]
    return opening + block + closing + text[m.end() :]


def as_list(value: Any) -> list[str]:
    """A property that may be a scalar, a list or empty, as a list of strings."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and v != ""]
    return [str(value)]


def rewrite_links(text: str, should_rewrite: Any, new_target: str) -> tuple[str, int]:
    """Point every wikilink whose target satisfies ``should_rewrite`` at ``new_target``.

    Headings, block refs, aliases and the ``!`` of embeds are kept.
    """
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        target = m.group(1).strip()
        if not target or not should_rewrite(target):
            return m.group(0)
        count += 1
        whole = m.group(0)
        start = whole.index("[[") + 2
        return whole[:start] + new_target + whole[start + len(m.group(1)) :]

    return _LINK_RE.sub(sub, text), count


def wikilinks(text: str) -> list[str]:
    """Link targets in order of appearance (without heading/alias), deduplicated."""
    seen: dict[str, None] = {}
    for m in _LINK_RE.finditer(text):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


def frontmatter_links(fm: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            found.extend(wikilinks(v))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(fm)
    return found


def tags(fm: dict[str, Any], body: str) -> list[str]:
    """Frontmatter ``tags`` plus inline ``#tags`` outside code fences, lowercased."""
    out = {t.lstrip("#").lower() for t in as_list(fm.get("tags"))}
    # A heading in a link ([[#Next batch]], [x](#Next)) or code isn't a tag.
    text = _NOT_TAGS_RE.sub(" ", _FENCE_RE.sub("", body))
    out.update(t.lower() for t in _TAG_RE.findall(text))
    return sorted(out)


def headings(body: str) -> list[tuple[int, int, str]]:
    """``(line_index, level, text)`` for each ATX heading outside code fences."""
    out = []
    in_fence = False
    for i, line in enumerate(body.split("\n")):
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def section(text: str, heading: str) -> tuple[int, int]:
    """``(heading line, end line)`` of ``heading``'s section in ``text``'s lines.

    The section runs to the next heading of the same or a higher level.
    Raises ``KeyError`` if there's no such heading.
    """
    want = heading.lstrip("#").strip().lower()
    hs = headings(text)
    for n, (idx, level, title) in enumerate(hs):
        if title.lower() == want:
            end = next((i for i, lv, _ in hs[n + 1 :] if lv <= level), None)
            return idx, len(text.split("\n")) if end is None else end
    raise KeyError(heading)


def append_under_heading(text: str, addition: str, heading: str | None) -> str:
    """Append ``addition`` at the end of the note, or of ``heading``'s section.

    A section ending in an empty template placeholder (``- `` or ``- [ ] ``) has
    the placeholder replaced instead of left above the new line.
    """
    addition = addition.rstrip("\n")
    if heading is None:
        return text.rstrip("\n") + "\n" + addition + "\n"
    lines = text.split("\n")
    want = heading.lstrip("#").strip().lower()
    hs = headings(text)
    for n, (idx, level, title) in enumerate(hs):
        if title.lower() != want:
            continue
        end = next((i for i, lv, _ in hs[n + 1 :] if lv <= level), len(lines))
        last = end
        while last > idx + 1 and not lines[last - 1].strip():
            last -= 1
        if last > idx + 1 and lines[last - 1].strip() in {"-", "- [ ]", "*"}:
            last -= 1
            del lines[last]
            end -= 1
        lines[last:last] = addition.split("\n")
        return "\n".join(lines)
    raise KeyError(heading)


# --- Templates ------------------------------------------------------------------

_MOMENT_TOKENS = re.compile(
    r"\[[^\]]*\]|YYYY|YY|MMMM|MMM|MM|M|dddd|ddd|DD|D|HH|H|mm|ss|A"
)


def moment_format(when: datetime | date, fmt: str) -> str:
    """Format ``when`` with the moment.js tokens Obsidian's date formats use."""
    if not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day)
    values = {
        "YYYY": f"{when.year:04d}",
        "YY": f"{when.year % 100:02d}",
        "MMMM": when.strftime("%B"),
        "MMM": when.strftime("%b"),
        "MM": f"{when.month:02d}",
        "M": str(when.month),
        "dddd": when.strftime("%A"),
        "ddd": when.strftime("%a"),
        "DD": f"{when.day:02d}",
        "D": str(when.day),
        "HH": f"{when.hour:02d}",
        "H": str(when.hour),
        "mm": f"{when.minute:02d}",
        "ss": f"{when.second:02d}",
        "A": "AM" if when.hour < 12 else "PM",
    }

    def sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        return tok[1:-1] if tok.startswith("[") else values[tok]

    return _MOMENT_TOKENS.sub(sub, fmt)


_TEMPLATE_VAR = re.compile(r"\{\{\s*(date|time|title)(?::([^}]*))?\s*\}\}")


def render_template(
    template: str,
    *,
    title: str,
    when: datetime,
    date_format: str = "YYYY-MM-DD",
    time_format: str = "HH:mm",
) -> str:
    """Fill ``{{title}}``, ``{{date[:fmt]}}`` and ``{{time[:fmt]}}`` like core Templates."""

    def sub(m: re.Match[str]) -> str:
        var, fmt = m.group(1), m.group(2)
        if var == "title":
            return title
        default = date_format if var == "date" else time_format
        return moment_format(when, (fmt or default).strip())

    return _TEMPLATE_VAR.sub(sub, template)
