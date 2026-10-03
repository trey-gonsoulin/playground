"""MCP server exposing genealogy tools via Streamable HTTP transport.

Two groups of tools:
- ``familysearch_*``: read-only queries against the FamilySearch Family Tree.
- ``tree_*``: build and maintain your own tree (JSON in S3, exported as GEDCOM),
  including importing people and pedigrees from FamilySearch.

Every tool returns a dict (bare list returns serialize poorly in some clients).
"""

import hashlib
import mimetypes
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cache
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from genealogy import _familysearch as fs
from genealogy._gedcom import from_gedcom, to_gedcom
from genealogy._models import Citation, Event, Person, Tree
from genealogy._storage import get_store

mcp = FastMCP(
    "genealogy",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    host="0.0.0.0",
)

_MAX_RESOURCE_BYTES = 25 * 1024 * 1024


@contextmanager
def _edit(tree_name: str) -> Generator[Tree]:
    """Load a tree, yield it for mutation, then save with a conditional write."""
    store = get_store()
    tree, version = store.load(tree_name)
    yield tree
    store.save(tree, version)


@cache
def _fs() -> fs.FamilySearchClient:
    """One client per container: reuses connections and the cached token."""
    return fs.FamilySearchClient()


# -- FamilySearch -------------------------------------------------------------------


@mcp.tool()
def familysearch_status() -> dict:
    """Check FamilySearch connectivity and which user the stored token belongs to.

    Returns the configured environment and, when authorized, the signed-in user's
    display name and their own Family Tree person id (a good root for imports).
    """
    client = _fs()
    try:
        user = client.current_user()
    except fs.NotAuthenticated as exc:
        return {"authenticated": False, "api": client.env.api, "detail": str(exc)}
    return {
        "authenticated": True,
        "api": client.env.api,
        "display_name": user.get("displayName"),
        "tree_person_id": user.get("personId"),
    }


@mcp.tool()
def familysearch_search(
    given_name: str | None = None,
    surname: str | None = None,
    sex: str | None = None,
    birth_date: str | None = None,
    birth_place: str | None = None,
    death_date: str | None = None,
    death_place: str | None = None,
    father_given_name: str | None = None,
    father_surname: str | None = None,
    mother_given_name: str | None = None,
    mother_surname: str | None = None,
    spouse_given_name: str | None = None,
    spouse_surname: str | None = None,
    count: int = 20,
) -> dict:
    """Search the FamilySearch Family Tree for people matching the given details.

    Dates may be a year ('1850') or a fuller date ('12 March 1850'); matching is
    fuzzy. Places are free text ('Cork, Ireland'). Provide as many details as
    you know — relatives' names sharply improve ranking.

    Args:
        sex: 'Male' or 'Female'.
        count: Max results (1-100, default 20).

    Returns {'total': int, 'results': [{pid, name, lifespan, birth, death, score, url}]}.
    Pass a pid to familysearch_get_person or tree_import_familysearch.
    """
    criteria = {
        "givenName": given_name,
        "surname": surname,
        "sex": sex,
        "birthLikeDate": birth_date,
        "birthLikePlace": birth_place,
        "deathLikeDate": death_date,
        "deathLikePlace": death_place,
        "fatherGivenName": father_given_name,
        "fatherSurname": father_surname,
        "motherGivenName": mother_given_name,
        "motherSurname": mother_surname,
        "spouseGivenName": spouse_given_name,
        "spouseSurname": spouse_surname,
    }
    data = _fs().search(count=count, **criteria)
    results = fs.search_results(data)
    return {"total": data.get("results", len(results)), "results": results}


@mcp.tool()
def familysearch_get_person(pid: str) -> dict:
    """Read one FamilySearch Family Tree person with facts and immediate family.

    Args:
        pid: FamilySearch person id, e.g. 'KWQ7-ABC'.

    Returns the person's summary, facts (as GEDCOM-style events), and summaries
    of parents, spouses and children (each with their own pid).
    """
    doc = _fs().person(pid)
    detail = fs.person_detail(doc or {}, pid)
    if detail is None:
        raise ValueError(f"FamilySearch person {pid!r} not found")
    return detail


@mcp.tool()
def familysearch_get_ancestry(pid: str, generations: int = 4) -> dict:
    """Read a FamilySearch pedigree (direct ancestors) rooted at a person.

    Args:
        pid: Root FamilySearch person id.
        generations: 1-8 (default 4).

    Returns {'ancestors': [...]} ordered by Ahnentafel number: 1 = root,
    2n = father of n, 2n+1 = mother of n.
    """
    data = _fs().ancestry(pid, generations) or {}
    ancestors = [
        {"ahnentafel": fs.ahnentafel(p), **fs.person_summary(p)}
        for p in data.get("persons", [])
    ]
    ancestors.sort(key=lambda a: a["ahnentafel"] or 10**9)
    return {"root": pid, "generations": generations, "ancestors": ancestors}


# -- Tree: read ------------------------------------------------------------------------


@mcp.tool()
def tree_list() -> dict:
    """List the family trees that have been saved."""
    return {"trees": get_store().list_trees()}


@mcp.tool()
def tree_summary(tree: str = "default") -> dict:
    """Overview of a tree: counts, most common surnames, and people with no parents.

    Args:
        tree: Tree name (default 'default').
    """
    t, _ = get_store().load(tree)
    surnames = Counter(p.surname for p in t.people.values() if p.surname)
    famc, _ = t.family_index()
    roots = [p.summary() for p in t.people.values() if p.id not in famc]
    return {
        "tree": t.name,
        "people": len(t.people),
        "families": len(t.families),
        "top_surnames": dict(surnames.most_common(15)),
        "people_without_parents": roots[:50],
    }


@mcp.tool()
def tree_search_people(query: str, tree: str = "default", limit: int = 50) -> dict:
    """Find people in your tree by name words (case-insensitive) or tree id.

    Args:
        query: e.g. 'mary walsh' or 'I12'.
    """
    matches = get_store().load(tree)[0].search(query)
    return {"count": len(matches), "people": [p.summary() for p in matches[:limit]]}


@mcp.tool()
def tree_get_person(person_id: str, tree: str = "default") -> dict:
    """Full record for one person in your tree, plus parents, siblings, partners, children."""
    t, _ = get_store().load(tree)
    person = t.person(person_id)
    return {
        "person": person.model_dump(exclude_defaults=True),
        **t.relatives(person_id),
    }


# -- Tree: write ------------------------------------------------------------------------


@mcp.tool()
def tree_add_person(
    given: str | None = None,
    surname: str | None = None,
    sex: str = "U",
    birth_date: str | None = None,
    birth_place: str | None = None,
    death_date: str | None = None,
    death_place: str | None = None,
    living: bool = False,
    note: str | None = None,
    familysearch_id: str | None = None,
    tree: str = "default",
) -> dict:
    """Add a person to your tree. Link them to relatives afterwards with tree_link.

    Args:
        sex: 'M', 'F' or 'U'.
        birth_date / death_date: GEDCOM date phrases, e.g. '12 MAR 1850',
            'ABT 1850', 'BEF 1900', 'BET 1850 AND 1855'.
        familysearch_id: Optional FamilySearch pid this person corresponds to.
    """
    with _edit(tree) as t:
        person = Person(
            id=t.new_person_id(),
            given=given,
            surname=surname,
            sex=sex,
            living=living,
            notes=[note] if note else [],
        )
        t.set_external_id(person, fs.SYSTEM, familysearch_id)
        if birth_date or birth_place:
            person.events.append(Event(type="BIRT", date=birth_date, place=birth_place))
        if death_date or death_place:
            person.events.append(Event(type="DEAT", date=death_date, place=death_place))
        t.people[person.id] = person
    return {"added": person.summary()}


@mcp.tool()
def tree_update_person(
    person_id: str,
    given: str | None = None,
    surname: str | None = None,
    sex: str | None = None,
    living: bool | None = None,
    add_note: str | None = None,
    familysearch_id: str | None = None,
    tree: str = "default",
) -> dict:
    """Change a person's name, sex, living flag or FamilySearch id, or append a note.

    Only the arguments you pass are changed. Use tree_set_event for dates/places.
    """
    with _edit(tree) as t:
        p = t.person(person_id)
        if given is not None:
            p.given = given or None
        if surname is not None:
            p.surname = surname or None
        if sex is not None:
            p.sex = sex
        if living is not None:
            p.living = living
        if add_note:
            p.notes.append(add_note)
        if familysearch_id is not None:
            t.set_external_id(p, fs.SYSTEM, familysearch_id)
    return {"updated": p.summary()}


@mcp.tool()
def tree_set_event(
    person_id: str,
    event_type: str,
    date: str | None = None,
    place: str | None = None,
    value: str | None = None,
    citation_title: str | None = None,
    citation_url: str | None = None,
    tree: str = "default",
) -> dict:
    """Record a life event for a person, optionally with a source citation.

    Args:
        event_type: GEDCOM tag — BIRT, CHR, BAPM, DEAT, BURI, CREM, RESI, OCCU,
            IMMI, EMIG, NATU, CENS. Birth/death/burial-type events replace the
            existing one; others (residence, census, occupation...) are appended.
        date: GEDCOM date phrase, e.g. '12 MAR 1850', 'ABT 1850'.
        value: Free text for value-carrying events, e.g. the occupation for OCCU.
        citation_title / citation_url: Where this fact came from.
    """
    event = Event(type=event_type, date=date, place=place, value=value)
    if citation_title or citation_url:
        event.citations.append(
            Citation(title=citation_title or citation_url or "", url=citation_url)
        )
    with _edit(tree) as t:
        p = t.person(person_id)
        p.set_event(event)
    return {"person": p.summary(), "event": event.model_dump(exclude_defaults=True)}


@mcp.tool()
def tree_link(
    relationship: str,
    person_id: str,
    other_id: str,
    tree: str = "default",
) -> dict:
    """Link two people already in your tree.

    Args:
        relationship: 'parent' (other_id is a parent of person_id) or
            'partner' (spouse/partner; creates their couple family).
    """
    with _edit(tree) as t:
        if relationship == "parent":
            fam = t.add_parent(person_id, other_id)
        elif relationship == "partner":
            fam = t.family_for_partners(person_id, other_id)
        else:
            raise ValueError("relationship must be 'parent' or 'partner'")
    return {"family": fam.model_dump(exclude_defaults=True)}


@mcp.tool()
def tree_remove_person(person_id: str, tree: str = "default") -> dict:
    """Delete a person and their links from your tree. Cannot be undone except via
    S3 object versioning."""
    with _edit(tree) as t:
        removed = t.person(person_id).summary()
        t.remove_person(person_id)
    return {"removed": removed}


@mcp.tool()
def tree_import_familysearch(
    pid: str,
    generations: int = 4,
    overwrite: bool = False,
    tree: str = "default",
) -> dict:
    """Copy a FamilySearch person and their direct ancestors into your tree.

    People are matched on FamilySearch id, so re-running is safe: existing
    people keep your local edits and only gain missing events, unless
    overwrite=True (which replaces names/events with FamilySearch's, keeping
    your notes and resources). Living people come through as FamilySearch
    shows them to you.

    Args:
        pid: Root FamilySearch person id.
        generations: 1-8 (default 4). 1 = just this person and their parents.
    """
    data = _fs().ancestry(pid, generations)
    if not data or not data.get("persons"):
        raise ValueError(f"FamilySearch returned no pedigree for {pid!r}")
    with _edit(tree) as t:
        stats = fs.import_ancestry(t, data, overwrite=overwrite)
        root = t.find_by_external_id(fs.SYSTEM, pid)
    return {"tree": tree, "root": root.summary() if root else None, **stats}


# -- Tree: GEDCOM + resources ------------------------------------------------------------


@mcp.tool()
def tree_export_gedcom(tree: str = "default", include_text: bool = False) -> dict:
    """Export the tree as GEDCOM 5.5.1 and save it as tree.ged (plus a dated copy).

    Returns a download URL (presigned, valid 1 hour). Set include_text=True to
    also return the GEDCOM inline (only sensible for small trees).
    """
    store = get_store()
    t, _ = store.load(tree)
    ged = to_gedcom(t).encode()
    dated = f"exports/tree-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.ged"
    store.put_file(tree, dated, ged, "text/x-gedcom")
    store.copy_file(tree, dated, "tree.ged")
    out = {
        "tree": tree,
        "people": len(t.people),
        "families": len(t.families),
        "bytes": len(ged),
        "url": store.file_url(tree, "tree.ged"),
    }
    if include_text:
        out["gedcom"] = ged.decode()
    return out


@mcp.tool()
def tree_import_gedcom(
    gedcom_text: str | None = None,
    stored_path: str | None = None,
    tree: str = "default",
    replace: bool = False,
) -> dict:
    """Load a GEDCOM file into a tree. Refuses to clobber a non-empty tree unless replace=True.

    Provide either gedcom_text (the file contents) or stored_path (a file already
    under the tree's storage, e.g. 'uploads/family.ged'). Ids are renumbered.
    The FamilySearch id is read from _FSFTID tags when present.
    """
    store = get_store()
    if stored_path:
        text = store.read_file(tree, stored_path).decode("utf-8-sig")
    elif gedcom_text:
        text = gedcom_text
    else:
        raise ValueError("Provide gedcom_text or stored_path")
    current, version = store.load(tree)
    if current.people and not replace:
        raise ValueError(
            f"Tree {tree!r} already has {len(current.people)} people; pass replace=True "
            "or import into a new tree name"
        )
    new = from_gedcom(text, name=tree)
    store.save(new, version)
    return {"tree": tree, "people": len(new.people), "families": len(new.families)}


@mcp.tool()
def tree_attach_resource_from_url(
    url: str,
    person_ids: list[str],
    filename: str | None = None,
    description: str | None = None,
    tree: str = "default",
) -> dict:
    """Download a document/image (record scan, obituary, photo) into the tree's
    resources and attach it to one or more people.

    Args:
        url: Public http(s) URL to fetch (max 25 MB).
        person_ids: Tree ids to attach it to.
        filename: Name to store it under; defaults to the URL's last path segment.
        description: Saved as a citation on each person.

    Files are stored content-addressed (resources/<hash>-<name>), so different
    documents never overwrite each other and retrying the same attach is a no-op.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("url must be http(s)")
    data = bytearray()
    with httpx.stream("GET", url, timeout=20, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            data += chunk
            if len(data) > _MAX_RESOURCE_BYTES:
                raise ValueError("Resource exceeds 25 MB")
        content_type = resp.headers.get("content-type")
    name = filename or parsed.path.rstrip("/").rsplit("/", 1)[-1] or "resource"
    content_type = content_type or mimetypes.guess_type(name)[0]
    digest = hashlib.sha256(data).hexdigest()[:16]
    rel = f"resources/{digest}-{name}"
    store = get_store()
    with _edit(tree) as t:
        people = [t.person(pid) for pid in person_ids]
        locator = store.put_file(
            tree, rel, bytes(data), content_type or "application/octet-stream"
        )
        for p in people:
            if rel in p.resources:
                continue
            p.resources.append(rel)
            p.citations.append(
                Citation(title=description or name, url=url, note=f"Saved copy: {rel}")
            )
    return {"stored": locator, "path": rel, "attached_to": person_ids}


@mcp.tool()
def tree_list_files(tree: str = "default", prefix: str = "") -> dict:
    """List stored files for a tree (tree.json, tree.ged, exports/, resources/...)."""
    return {"files": get_store().list_files(tree, prefix)}


@mcp.tool()
def tree_file_url(path: str, tree: str = "default") -> dict:
    """Get a temporary (1 hour) download URL for a stored file, e.g. 'tree.ged'."""
    return {"path": path, "url": get_store().file_url(tree, path)}
