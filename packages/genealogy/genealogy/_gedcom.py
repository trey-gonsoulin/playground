"""GEDCOM 5.5.1 export/import for the canonical Tree model.

Only the subset the model carries is written or read: names, sex, events,
notes, citations (as inline SOUR text), parent/partner links, and the
FamilySearch id via the de-facto ``_FSFTID`` tag (understood by RootsMagic,
Ancestral Quest and others). Unknown tags are ignored on import.
"""

from dataclasses import dataclass, field

from genealogy._models import EVENT_TAGS, Citation, Event, Family, Person, Tree

_MAX_LINE = 240  # GEDCOM 5.5.1 caps lines at 255 chars including level/tag.
_FS_TAG = "_FSFTID"


# -- export -------------------------------------------------------------------


def to_gedcom(tree: Tree, source_name: str = "genealogy-mcp") -> str:
    out: list[str] = [
        "0 HEAD",
        f"1 SOUR {source_name}",
        "2 VERS 0.1.0",
        "1 GEDC",
        "2 VERS 5.5.1",
        "2 FORM LINEAGE-LINKED",
        "1 CHAR UTF-8",
    ]
    famc, fams = tree.family_index()
    for person in tree.people.values():
        out += _person_lines(person, famc.get(person.id, []), fams.get(person.id, []))
    for fam in tree.families.values():
        out += _family_lines(tree, fam)
    out.append("0 TRLR")
    return "\n".join(out) + "\n"


def _xref(id_: str) -> str:
    return f"@{id_}@"


def _chunks(para: str) -> list[str]:
    """Split a line into CONC-sized pieces without cutting next to a space.

    Many readers trim whitespace around line values, so a piece that starts or
    ends with a space would lose it on import (joining two words); per the spec
    we back the cut off to a non-space boundary instead.
    """
    chunks = []
    while len(para) > _MAX_LINE:
        cut = _MAX_LINE
        while cut > 1 and (para[cut - 1] == " " or para[cut] == " "):
            cut -= 1
        if cut <= 1:  # a long run of spaces: no clean boundary, cut anyway
            cut = _MAX_LINE
        chunks.append(para[:cut])
        para = para[cut:]
    return [*chunks, para]


def _text(level: int, tag: str, value: str) -> list[str]:
    """Emit a text value, splitting newlines into CONT and long runs into CONC."""
    lines: list[str] = []
    for i, para in enumerate(value.split("\n")):
        head, *rest = _chunks(para)
        head_tag, head_level = (tag, level) if i == 0 else ("CONT", level + 1)
        lines.append(
            f"{head_level} {head_tag} {head}" if head else f"{head_level} {head_tag}"
        )
        lines += [f"{level + 1} CONC {c}" for c in rest]
    return lines


def _citation_lines(level: int, cit: Citation) -> list[str]:
    lines = _text(level, "SOUR", cit.title)
    if cit.url:
        lines.append(f"{level + 1} PAGE {cit.url}")
    if cit.note:
        lines += _text(level + 1, "NOTE", cit.note)
    return lines


def _event_lines(event: Event) -> list[str]:
    lines = [f"1 {event.type} {event.value or ''}".rstrip()]
    if event.date:
        lines.append(f"2 DATE {event.date}")
    if event.place:
        lines.append(f"2 PLAC {event.place}")
    for cit in event.citations:
        lines += _citation_lines(2, cit)
    return lines


def _person_lines(p: Person, famc: list[str], fams: list[str]) -> list[str]:
    lines = [f"0 {_xref(p.id)} INDI"]
    lines.append(f"1 NAME {p.given or ''} /{p.surname or ''}/".replace("  ", " "))
    if p.given:
        lines.append(f"2 GIVN {p.given}")
    if p.surname:
        lines.append(f"2 SURN {p.surname}")
    lines.append(f"1 SEX {p.sex}")
    for event in p.events:
        lines += _event_lines(event)
    for note in p.notes:
        lines += _text(1, "NOTE", note)
    for cit in p.citations:
        lines += _citation_lines(1, cit)
    if fs_id := p.external_ids.get("familysearch"):
        lines.append(f"1 {_FS_TAG} {fs_id}")
    lines += [f"1 FAMC {_xref(f)}" for f in famc]
    lines += [f"1 FAMS {_xref(f)}" for f in fams]
    return lines


_ROLE_ORDER = {"M": 0, "U": 1, "F": 2}


def _family_lines(tree: Tree, fam: Family) -> list[str]:
    lines = [f"0 {_xref(fam.id)} FAM"]
    # 5.5.1 only has HUSB/WIFE roles: order partners M, U, F and take the first
    # as HUSB and the second as WIFE; a lone partner is WIFE only if female.
    partners = sorted(
        (tree.people[i] for i in fam.partner_ids if i in tree.people),
        key=lambda p: _ROLE_ORDER[p.sex],
    )
    roles = (
        ["WIFE"] if len(partners) == 1 and partners[0].sex == "F" else ["HUSB", "WIFE"]
    )
    lines += [f"1 {role} {_xref(p.id)}" for role, p in zip(roles, partners)]
    for child in fam.child_ids:
        lines.append(f"1 CHIL {_xref(child)}")
    for event in fam.events:
        lines += _event_lines(event)
    for note in fam.notes:
        lines += _text(1, "NOTE", note)
    return lines


# -- import -------------------------------------------------------------------


@dataclass
class _Node:
    tag: str
    value: str = ""
    xref: str | None = None
    children: list["_Node"] = field(default_factory=list)

    def first(self, tag: str) -> "_Node | None":
        return next((c for c in self.children if c.tag == tag), None)

    def all(self, tag: str) -> list["_Node"]:
        return [c for c in self.children if c.tag == tag]

    def text(self) -> str:
        """Value with CONT/CONC continuations folded in."""
        parts = [self.value]
        for c in self.children:
            if c.tag == "CONT":
                parts.append("\n" + c.value)
            elif c.tag == "CONC":
                parts.append(c.value)
        return "".join(parts)


def _parse_nodes(text: str) -> list[_Node]:
    roots: list[_Node] = []
    stack: list[tuple[int, _Node]] = []
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        level_s, _, rest = line.lstrip().partition(" ")
        level = int(level_s)
        xref = None
        if rest.startswith("@"):
            xref, _, rest = rest.partition(" ")
            xref = xref.strip("@")
        tag, _, value = rest.partition(" ")
        node = _Node(tag=tag, value=value, xref=xref)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    return roots


def _parse_citations(node: _Node) -> list[Citation]:
    cits = []
    for s in node.all("SOUR"):
        page = s.first("PAGE")
        note = s.first("NOTE")
        cits.append(
            Citation(
                title=s.text() or "(untitled source)",
                url=page.value if page else None,
                note=note.text() if note else None,
                origin="gedcom",
            )
        )
    return cits


def _parse_event(node: _Node) -> Event:
    date, place = node.first("DATE"), node.first("PLAC")
    return Event(
        type=node.tag,
        value=node.value if node.value not in ("Y", "") else None,
        date=date.value if date else None,
        place=place.value if place else None,
        citations=_parse_citations(node),
    )


def _split_name(value: str) -> tuple[str | None, str | None]:
    if "/" in value:
        given, _, rest = value.partition("/")
        surname = rest.partition("/")[0]
        return given.strip() or None, surname.strip() or None
    return value.strip() or None, None


def _events(node: _Node) -> list[Event]:
    return [_parse_event(c) for c in node.children if c.tag in EVENT_TAGS]


def _notes(node: _Node) -> list[str]:
    # "@N1@"-style values point at shared NOTE records, which we don't import.
    return [n.text() for n in node.all("NOTE") if not n.value.startswith("@")]


def from_gedcom(text: str, name: str = "default") -> Tree:
    """Parse GEDCOM text into a Tree. People and families are renumbered."""
    roots = _parse_nodes(text)
    tree = Tree(name=name)
    id_map: dict[str, str] = {}

    def _refs(nodes: list[_Node]) -> list[str]:
        return [id_map[x] for c in nodes if (x := c.value.strip("@")) in id_map]

    for node in roots:
        if node.tag != "INDI" or not node.xref:
            continue
        given, surname = None, None
        if name_node := node.first("NAME"):
            given, surname = _split_name(name_node.value)
            if g := name_node.first("GIVN"):
                given = g.value
            if s := name_node.first("SURN"):
                surname = s.value
        sex = node.first("SEX")
        person = Person(
            id=tree.new_person_id(),
            given=given,
            surname=surname,
            sex=sex.value if sex else "U",
            events=_events(node),
            notes=_notes(node),
            citations=_parse_citations(node),
        )
        if fs := node.first(_FS_TAG):
            person.external_ids["familysearch"] = fs.value.strip()
        id_map[node.xref] = person.id
        tree.people[person.id] = person

    for node in roots:
        if node.tag != "FAM" or not node.xref:
            continue
        fam = Family(
            id=tree.new_family_id(),
            partner_ids=_refs(node.all("HUSB") + node.all("WIFE")),
            child_ids=_refs(node.all("CHIL")),
            events=_events(node),
            notes=_notes(node),
        )
        tree.families[fam.id] = fam

    return tree
