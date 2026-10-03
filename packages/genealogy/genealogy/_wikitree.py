"""WikiTree API client and WikiTree profile -> Person mapping.

WikiTree (https://www.wikitree.com) is a free collaborative world tree whose
read API needs no account for public profiles:
https://github.com/wikitree/wikitree-api

All calls are GET ``https://api.wikitree.com/api.php?action=...`` and return a
top-level JSON array whose first element holds the payload. Requests carry an
``appId`` (``WIKITREE_APP_ID``) because anonymous apps are rate limited harder.

Profiles are keyed by their WikiTree ID (``Name``, e.g. ``Clemens-1``); the
numeric ``Id`` only links records within one response (``Father``/``Mother``).
"""

import calendar
import os
from functools import cache

import httpx

from genealogy._models import Citation, Event, Family, Person, Tree

API = "https://api.wikitree.com/api.php"
SYSTEM = "wikitree"
FIELDS = ",".join(
    [
        "Id",
        "Name",
        "FirstName",
        "MiddleName",
        "LastNameAtBirth",
        "LastNameCurrent",
        "BirthDate",
        "DeathDate",
        "BirthLocation",
        "DeathLocation",
        "Gender",
        "IsLiving",
        "Father",
        "Mother",
        "DataStatus",
    ]
)
# DataStatus value for a date -> GEDCOM qualifier.
_QUALIFIERS = {"guess": "ABT", "before": "BEF", "after": "AFT"}


class WikiTreeError(RuntimeError):
    pass


@cache
def _http() -> httpx.Client:
    """One pooled client per process (reused across warm Lambda invocations)."""
    return httpx.Client(timeout=20, follow_redirects=True)


def _call(action: str, **params: str | int | None) -> dict:
    query = {k: v for k, v in params.items() if v not in (None, "")}
    query |= {
        "action": action,
        "appId": os.environ.get("WIKITREE_APP_ID", "genealogy-mcp"),
    }
    resp = _http().get(API, params=query)
    resp.raise_for_status()
    data = resp.json()
    payload = data[0] if isinstance(data, list) and data else data
    if not isinstance(payload, dict):
        raise WikiTreeError(f"Unexpected WikiTree response: {str(data)[:200]}")
    status = payload.get("status")
    if status not in (0, "0", None, ""):
        raise WikiTreeError(f"WikiTree {action} failed: {status}")
    return payload


# -- endpoints -----------------------------------------------------------------


def search(limit: int = 20, **criteria: str | None) -> dict:
    """searchPerson. Criteria keys are WikiTree's: FirstName, LastName,
    BirthDate/DeathDate (YYYY-MM-DD or YYYY), BirthLocation, DeathLocation,
    Gender, fatherFirstName, fatherLastName, motherFirstName, motherLastName."""
    if not any(criteria.values()):
        raise ValueError("Provide at least one search criterion")
    return _call(
        "searchPerson", limit=max(1, min(limit, 100)), fields=FIELDS, **criteria
    )


def relatives(wikitree_id: str) -> dict | None:
    """The profile with Parents, Spouses (incl. marriage data) and Children."""
    payload = _call(
        "getRelatives",
        keys=wikitree_id,
        getParents=1,
        getSpouses=1,
        getChildren=1,
        fields=FIELDS,
    )
    items = payload.get("items") or []
    return (items[0] or {}).get("person") if items else None


def ancestors(wikitree_id: str, generations: int = 4) -> list[dict]:
    """The profile (first) plus its direct ancestors, each with Father/Mother Ids.

    Uses getPeople: the older getAncestors still answers but flags itself
    deprecated in ``status``. getPeople returns ``people`` keyed by numeric Id
    and ``resultByKey`` mapping the requested key to the root's Id.
    """
    payload = _call(
        "getPeople",
        keys=wikitree_id,
        ancestors=max(1, min(generations, 10)),
        fields=FIELDS,
    )
    people = list((payload.get("people") or {}).values())
    root_id = ((payload.get("resultByKey") or {}).get(wikitree_id) or {}).get("Id")
    return sorted(people, key=lambda p: p.get("Id") != root_id)


# -- mapping ---------------------------------------------------------------------


def profile_url(wikitree_id: str | None) -> str:
    return f"https://www.wikitree.com/wiki/{wikitree_id}"


def to_gedcom_date(date: str | None, status: str | None = None) -> str | None:
    """'1835-11-30' -> '30 NOV 1835'; zeros mean unknown ('1850-00-00' -> '1850').

    ``status`` is the WikiTree DataStatus for the date: guess/before/after become
    ABT/BEF/AFT.
    """
    if not date:
        return None
    year, _, rest = date.partition("-")
    month, _, day = rest.partition("-")
    if not year.isdigit() or int(year) == 0:
        return None
    out = str(int(year))
    if month.isdigit() and 1 <= int(month) <= 12:
        out = f"{calendar.month_abbr[int(month)].upper()} {out}"
        if day.isdigit() and int(day):
            out = f"{int(day)} {out}"
    qualifier = _QUALIFIERS.get(status or "")
    return f"{qualifier} {out}" if qualifier else out


def _event(profile: dict, kind: str, tag: str) -> Event | None:
    status = (profile.get("DataStatus") or {}).get(f"{kind}Date")
    date = to_gedcom_date(profile.get(f"{kind}Date"), status)
    place = profile.get(f"{kind}Location") or None
    return Event(type=tag, date=date, place=place) if date or place else None


def summary(profile: dict) -> dict:
    """Compact view of a WikiTree profile for tool output."""
    status = profile.get("DataStatus") or {}
    birth = to_gedcom_date(profile.get("BirthDate"), status.get("BirthDate"))
    death = to_gedcom_date(profile.get("DeathDate"), status.get("DeathDate"))
    name = " ".join(
        p
        for p in (
            profile.get("FirstName"),
            profile.get("MiddleName"),
            profile.get("LastNameAtBirth") or profile.get("LastNameCurrent"),
        )
        if p
    )
    return {
        "wikitree_id": profile.get("Name"),
        "name": name or None,
        "gender": profile.get("Gender") or None,
        "birth": ", ".join(x for x in (birth, profile.get("BirthLocation")) if x)
        or None,
        "death": ", ".join(x for x in (death, profile.get("DeathLocation")) if x)
        or None,
        "married_name": profile.get("LastNameCurrent")
        if profile.get("LastNameCurrent") != profile.get("LastNameAtBirth")
        else None,
        "url": profile_url(profile.get("Name")),
    }


def to_person(profile: dict, person_id: str) -> Person:
    wt_id = profile["Name"]
    given = " ".join(
        p for p in (profile.get("FirstName"), profile.get("MiddleName")) if p
    )
    return Person(
        id=person_id,
        given=given or None,
        surname=profile.get("LastNameAtBirth")
        or profile.get("LastNameCurrent")
        or None,
        sex=profile.get("Gender") or "U",
        living=bool(int(profile.get("IsLiving") or 0)),
        events=[
            e
            for e in (
                _event(profile, "Birth", "BIRT"),
                _event(profile, "Death", "DEAT"),
            )
            if e
        ],
        external_ids={SYSTEM: wt_id},
        citations=[
            Citation(
                title=f"WikiTree profile {wt_id}", url=profile_url(wt_id), origin=SYSTEM
            )
        ],
    )


def detail(person: dict) -> dict:
    """Profile summary plus parents, spouses (with marriage) and children."""

    def _group(key: str) -> list[dict]:
        return [summary(p) for p in (person.get(key) or {}).values()]

    spouses = []
    for p in (person.get("Spouses") or {}).values():
        married = to_gedcom_date(p.get("marriage_date"))
        place = p.get("marriage_location") or None
        marriage = ", ".join(x for x in (married, place) if x) or None
        spouses.append({**summary(p), "marriage": marriage})
    return {
        **summary(person),
        "parents": _group("Parents"),
        "spouses": spouses,
        "children": _group("Children"),
    }


def ancestors_tree(profiles: list[dict]) -> Tree:
    """Build a Tree from getAncestors output, linking Father/Mother by numeric Id."""
    tree = Tree(name="wikitree")
    by_num: dict[int, str] = {}
    for profile in profiles:
        if not profile.get("Name"):
            continue  # private profiles can come back without an id
        person = to_person(profile, tree.new_person_id())
        tree.people[person.id] = person
        by_num[profile["Id"]] = person.id
    for profile in profiles:
        child = by_num.get(profile.get("Id", 0))
        parents = [
            by_num[p]
            for p in (profile.get("Father"), profile.get("Mother"))
            if p in by_num
        ]
        if child and parents:
            fam = Family(
                id=tree.new_family_id(), partner_ids=parents, child_ids=[child]
            )
            tree.families[fam.id] = fam
    return tree
