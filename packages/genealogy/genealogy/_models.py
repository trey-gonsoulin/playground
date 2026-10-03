"""Canonical family-tree model.

The tree is stored as JSON (``tree.json``) and exported to GEDCOM on demand.
JSON is the source of truth because it carries provenance (external ids,
citations) losslessly; GEDCOM is the interchange format.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# GEDCOM event tags the model supports -> what they mean.
EVENT_TAGS = {
    "BIRT": "Birth",
    "CHR": "Christening",
    "BAPM": "Baptism",
    "DEAT": "Death",
    "BURI": "Burial",
    "CREM": "Cremation",
    "RESI": "Residence",
    "OCCU": "Occupation",
    "IMMI": "Immigration",
    "EMIG": "Emigration",
    "NATU": "Naturalization",
    "CENS": "Census",
    "MARR": "Marriage",
    "DIV": "Divorce",
}
# Events a person has at most once; set_event replaces these instead of appending.
SINGLE_EVENTS = {"BIRT", "CHR", "BAPM", "DEAT", "BURI", "CREM"}


class Citation(BaseModel):
    title: str
    url: str | None = None
    # Which system the citation came from, e.g. "wikitree", "gedcom", "manual".
    origin: str = "manual"
    note: str | None = None


class Event(BaseModel):
    # GEDCOM tag (BIRT, DEAT, MARR, ...). See EVENT_TAGS.
    type: str
    # GEDCOM-style date phrase, e.g. "12 MAR 1850", "ABT 1850", "BET 1850 AND 1855".
    date: str | None = None
    place: str | None = None
    # Free text value, e.g. the occupation itself for OCCU.
    value: str | None = None
    citations: list[Citation] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def _known_tag(cls, v: str) -> str:
        tag = str(v).upper()
        if tag not in EVENT_TAGS:
            raise ValueError(
                f"Unknown event type {v!r}; use one of {', '.join(EVENT_TAGS)}"
            )
        return tag


class Person(BaseModel):
    # Re-run validators on attribute assignment so `person.sex = "female"` normalizes.
    model_config = ConfigDict(validate_assignment=True)

    id: str
    given: str | None = None
    surname: str | None = None
    # Always one of M / F / U (GEDCOM SEX values); see _normalize_sex.
    sex: str = "U"
    living: bool = False
    events: list[Event] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    # System name -> id in that system, e.g. {"wikitree": "Clemens-1"}.
    external_ids: dict[str, str] = Field(default_factory=dict)
    # Storage paths (under the tree root) of resources attached to this person.
    resources: list[str] = Field(default_factory=list)

    @field_validator("sex", mode="before")
    @classmethod
    def _normalize_sex(cls, v: str | None) -> str:
        # Accepts M/F/U as well as Male/Female and blanks.
        first = (v or "U").strip()[:1].upper()
        return first if first in ("M", "F") else "U"

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.given, self.surname) if p) or "(unknown)"

    def event(self, type_: str) -> Event | None:
        return next((e for e in self.events if e.type == type_), None)

    def set_event(self, event: Event) -> None:
        """Add an event, replacing the existing one for single-occurrence types."""
        if event.type in SINGLE_EVENTS:
            self.events = [e for e in self.events if e.type != event.type]
        self.events.append(event)

    def summary(self) -> dict:
        birth, death = self.event("BIRT"), self.event("DEAT")
        return {
            "id": self.id,
            "name": self.display_name,
            "sex": self.sex,
            "birth": _event_phrase(birth),
            "death": _event_phrase(death),
            "external_ids": self.external_ids,
        }


class Family(BaseModel):
    """A couple (zero, one or two partners) and their children."""

    id: str
    partner_ids: list[str] = Field(default_factory=list)
    child_ids: list[str] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Tree(BaseModel):
    name: str = "default"
    people: dict[str, Person] = Field(default_factory=dict)
    families: dict[str, Family] = Field(default_factory=dict)
    next_person: int = 1
    next_family: int = 1

    # -- ids -----------------------------------------------------------------

    def new_person_id(self) -> str:
        pid = f"I{self.next_person}"
        self.next_person += 1
        return pid

    def new_family_id(self) -> str:
        fid = f"F{self.next_family}"
        self.next_family += 1
        return fid

    # -- lookups -------------------------------------------------------------

    def person(self, person_id: str) -> Person:
        if person_id not in self.people:
            raise ValueError(f"No person {person_id!r} in tree {self.name!r}")
        return self.people[person_id]

    def find_by_external_id(self, system: str, ext_id: str) -> Person | None:
        return next(
            (p for p in self.people.values() if p.external_ids.get(system) == ext_id),
            None,
        )

    def parent_families(self, person_id: str) -> list[Family]:
        return [f for f in self.families.values() if person_id in f.child_ids]

    def partner_families(self, person_id: str) -> list[Family]:
        return [f for f in self.families.values() if person_id in f.partner_ids]

    def family_index(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """(person -> families they're a child in, person -> families they're a partner in).

        One pass over families, for whole-tree walks where calling
        parent_families/partner_families per person would be O(people x families).
        """
        famc: dict[str, list[str]] = {}
        fams: dict[str, list[str]] = {}
        for fam in self.families.values():
            for c in fam.child_ids:
                famc.setdefault(c, []).append(fam.id)
            for p in fam.partner_ids:
                fams.setdefault(p, []).append(fam.id)
        return famc, fams

    def search(self, query: str) -> list[Person]:
        terms = query.lower().split()
        return [
            p
            for p in self.people.values()
            if all(t in p.display_name.lower() or t == p.id.lower() for t in terms)
        ]

    # -- mutations -----------------------------------------------------------

    def set_external_id(self, person: Person, system: str, ext_id: str | None) -> None:
        """Set (or clear, with a falsy id) an external id, keeping it unique in the tree."""
        if not ext_id:
            person.external_ids.pop(system, None)
            return
        dup = self.find_by_external_id(system, ext_id)
        if dup is not None and dup.id != person.id:
            raise ValueError(f"{system} id {ext_id} is already in the tree as {dup.id}")
        person.external_ids[system] = ext_id

    def _find_family(self, partners: set[str]) -> Family | None:
        return next(
            (f for f in self.families.values() if set(f.partner_ids) == partners), None
        )

    def family_for_partners(self, a: str | None, b: str | None) -> Family:
        """Return the family with exactly these partners, creating it if needed."""
        wanted = {x for x in (a, b) if x}
        for pid in wanted:
            self.person(pid)
        fam = self._find_family(wanted)
        if fam is None:
            fam = Family(id=self.new_family_id(), partner_ids=sorted(wanted))
            self.families[fam.id] = fam
        return fam

    def add_child(
        self, child_id: str, parent_a: str | None, parent_b: str | None
    ) -> Family:
        """Make these the child's parents, upgrading a less complete parent link.

        - Already in a family with (at least) these parents: no change.
        - In a family with only some of them (e.g. just the father was known):
          that link is upgraded. The family is widened in place only when this
          child is its sole child and the couple has no family yet; otherwise the
          child moves to the couple's family, so siblings (possibly half-siblings)
          keep their recorded parents and no duplicate couple is created.
        """
        wanted = {x for x in (parent_a, parent_b) if x}
        current = self.parent_families(child_id)
        for fam in current:
            if wanted <= set(fam.partner_ids):
                return fam
        partial = next((f for f in current if set(f.partner_ids) < wanted), None)
        couple = self._find_family(wanted)
        if partial and couple is None and partial.child_ids == [child_id]:
            for pid in wanted:
                self.person(pid)
            partial.partner_ids = sorted(wanted)
            return partial
        fam = couple or self.family_for_partners(parent_a, parent_b)
        if child_id not in fam.child_ids:
            fam.child_ids.append(child_id)
        if partial:
            partial.child_ids.remove(child_id)
            self._drop_if_empty(partial)
        return fam

    def add_parent(self, child_id: str, parent_id: str) -> Family:
        """Link one parent, pairing them with the child's sole known parent if any."""
        self.person(child_id)
        self.person(parent_id)
        current = self.parent_families(child_id)
        for fam in current:
            if parent_id in fam.partner_ids:
                return fam
        single = next((f for f in current if len(f.partner_ids) == 1), None)
        other = single.partner_ids[0] if single else None
        return self.add_child(child_id, other, parent_id)

    def _drop_if_empty(self, fam: Family) -> None:
        """Remove a family left with no children and no couple or recorded data."""
        if (
            not fam.child_ids
            and len(fam.partner_ids) < 2
            and not (fam.events or fam.notes)
        ):
            del self.families[fam.id]

    def own_id_system(self) -> str:
        """External-id system name under which GEDCOM exports carry this tree's ids,
        so an export edited in another app merges back onto the same people."""
        return f"tree:{self.name}"

    def merge_tree(self, incoming: "Tree", overwrite: bool = False) -> dict:
        """Merge another tree (a WikiTree pedigree, a GEDCOM import...) into this one.

        People match when they share any external id (same system and value),
        so re-importing the same source is idempotent. Unmatched people are
        added with fresh ids. Matched people keep local edits and only gain
        missing data, unless ``overwrite`` is set: then the incoming record wins,
        keeping local notes, resources and other systems' ids. Family links are
        then applied through add_child / family_for_partners, which never
        downgrade or duplicate existing links.
        """
        own = self.own_id_system()
        index = {
            (system, ext): p
            for p in self.people.values()
            for system, ext in [*p.external_ids.items(), (own, p.id)]
        }
        id_map: dict[str, str] = {}
        created = 0
        for incoming_id, original in incoming.people.items():
            fresh = original.model_copy(deep=True)
            keys = list(fresh.external_ids.items())
            existing = next((index[k] for k in keys if k in index), None)
            # A round-tripped export of this very tree: its own ids aren't external.
            fresh.external_ids.pop(own, None)
            if existing is None:
                fresh.id = self.new_person_id()
                self.people[fresh.id] = person = fresh
                created += 1
            else:
                person = self._merge_person(existing, fresh, overwrite)
            index.update({k: person for k in person.external_ids.items()})
            id_map[incoming_id] = person.id

        links = 0
        for fam in incoming.families.values():
            partners = [id_map[p] for p in fam.partner_ids if p in id_map][:2]
            if not partners:
                continue
            a, b = (partners + [None])[:2]
            children = [id_map[c] for c in fam.child_ids if c in id_map]
            targets = [self.add_child(c, a, b) for c in children]
            if not children:
                targets = [self.family_for_partners(a, b)]
            for target in dict.fromkeys(f.id for f in targets):
                merged_fam = self.families[target]
                merged_fam.events += [
                    e for e in fam.events if not _has_event(merged_fam.events, e)
                ]
                merged_fam.notes += [n for n in fam.notes if n not in merged_fam.notes]
            links += 1
        return {
            "created": created,
            "updated": len(id_map) - created,
            "families_linked": links,
        }

    def _merge_person(self, existing: Person, fresh: Person, overwrite: bool) -> Person:
        if overwrite:
            fresh.id = existing.id
            fresh.notes, fresh.resources = existing.notes, existing.resources
            fresh.external_ids = {**existing.external_ids, **fresh.external_ids}
            self.people[fresh.id] = fresh
            return fresh
        have = {e.type for e in existing.events}
        existing.events += [
            e
            for e in fresh.events
            if e.type not in have
            or (e.type not in SINGLE_EVENTS and not _has_event(existing.events, e))
        ]
        existing.notes += [n for n in fresh.notes if n not in existing.notes]
        cited = {(c.title, c.url) for c in existing.citations}
        existing.citations += [
            c for c in fresh.citations if (c.title, c.url) not in cited
        ]
        existing.external_ids = {**fresh.external_ids, **existing.external_ids}
        existing.given = existing.given or fresh.given
        existing.surname = existing.surname or fresh.surname
        if existing.sex == "U":
            existing.sex = fresh.sex
        return existing

    def remove_person(self, person_id: str) -> None:
        self.people.pop(person_id, None)
        for fam in list(self.families.values()):
            fam.partner_ids = [p for p in fam.partner_ids if p != person_id]
            fam.child_ids = [c for c in fam.child_ids if c != person_id]
            if not fam.partner_ids and not fam.child_ids:
                del self.families[fam.id]

    def relatives(self, person_id: str) -> dict[str, list[dict]]:
        parents, siblings, partners, children = [], [], [], []
        for fam in self.parent_families(person_id):
            parents += fam.partner_ids
            siblings += [c for c in fam.child_ids if c != person_id]
        for fam in self.partner_families(person_id):
            partners += [p for p in fam.partner_ids if p != person_id]
            children += fam.child_ids

        def _summaries(ids: list[str]) -> list[dict]:
            return [
                self.people[i].summary() for i in dict.fromkeys(ids) if i in self.people
            ]

        return {
            "parents": _summaries(parents),
            "siblings": _summaries(siblings),
            "partners": _summaries(partners),
            "children": _summaries(children),
        }


def _has_event(events: list[Event], event: Event) -> bool:
    """Same fact already recorded? Ignores citations, whose provenance fields
    don't survive a GEDCOM round trip."""
    key = (event.type, event.date, event.place, event.value)
    return any((e.type, e.date, e.place, e.value) == key for e in events)


def _event_phrase(event: Event | None) -> str | None:
    if event is None:
        return None
    return ", ".join(x for x in (event.date, event.place) if x) or None
