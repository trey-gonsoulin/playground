"""MCP server exposing genealogy tools via Streamable HTTP transport.

Tool groups:
- ``wikitree_*``: read-only queries against WikiTree's free public world tree.
- ``newspapers_search``: full-text search of historic US newspapers (Library
  of Congress, Chronicling America).
- ``tree_*``: build and maintain your own tree (JSON in S3, exported as GEDCOM),
  including importing WikiTree pedigrees and merging GEDCOM exports from other
  apps (Ancestry, RootsMagic...).

Every tool returns a dict (bare list returns serialize poorly in some clients).
"""

import hashlib
import mimetypes
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from genealogy import _newspapers as newspapers
from genealogy import _wikitree as wikitree
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


# -- WikiTree --------------------------------------------------------------------------


@mcp.tool()
def wikitree_search(
    first_name: str | None = None,
    last_name: str | None = None,
    birth_date: str | None = None,
    death_date: str | None = None,
    birth_location: str | None = None,
    death_location: str | None = None,
    gender: str | None = None,
    father_first_name: str | None = None,
    father_last_name: str | None = None,
    mother_first_name: str | None = None,
    mother_last_name: str | None = None,
    limit: int = 20,
) -> dict:
    """Search WikiTree's public world tree for people matching the given details.

    Matching is fuzzy (name variants, nearby dates). Parents' names sharply
    improve ranking.

    Args:
        last_name: Matches birth or married surname.
        birth_date / death_date: 'YYYY-MM-DD' or 'YYYY'.
        gender: 'Male' or 'Female'.
        limit: Max results (1-100, default 20).

    Returns {'total', 'results': [{wikitree_id, name, birth, death, url, ...}]}.
    Pass a wikitree_id to wikitree_get_person or tree_import_wikitree.
    """
    data = wikitree.search(
        limit=limit,
        FirstName=first_name,
        LastName=last_name,
        BirthDate=birth_date,
        DeathDate=death_date,
        BirthLocation=birth_location,
        DeathLocation=death_location,
        Gender=gender,
        fatherFirstName=father_first_name,
        fatherLastName=father_last_name,
        motherFirstName=mother_first_name,
        motherLastName=mother_last_name,
    )
    matches = data.get("matches") or []
    return {
        "total": data.get("total", len(matches)),
        "results": [wikitree.summary(m) for m in matches],
    }


@mcp.tool()
def wikitree_get_person(wikitree_id: str) -> dict:
    """Read one WikiTree profile with parents, spouses (with marriage) and children.

    Args:
        wikitree_id: e.g. 'Clemens-1' (the part after /wiki/ in a profile URL).
    """
    person = wikitree.relatives(wikitree_id)
    if not person:
        raise ValueError(f"WikiTree profile {wikitree_id!r} not found or private")
    return wikitree.detail(person)


@mcp.tool()
def wikitree_get_ancestors(wikitree_id: str, generations: int = 4) -> dict:
    """Read a WikiTree pedigree (direct ancestors) rooted at a profile.

    Args:
        generations: 1-10 (default 4). 1 = parents only.
    """
    profiles = wikitree.ancestors(wikitree_id, generations)
    by_num = {p.get("Id"): p.get("Name") for p in profiles}
    return {
        "root": wikitree_id,
        "ancestors": [
            {
                **wikitree.summary(p),
                "father": by_num.get(p.get("Father")),
                "mother": by_num.get(p.get("Mother")),
            }
            for p in profiles
        ],
    }


# -- Newspapers -------------------------------------------------------------------------


@mcp.tool()
def newspapers_search(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    state: str | None = None,
    exact_phrase: bool = True,
    count: int = 20,
    page: int = 1,
) -> dict:
    """Full-text search of historic US newspaper pages (1770–1963) at the Library
    of Congress — obituaries, marriage notices, court and society columns.

    Text is OCR, so spelling is noisy: try name variants, and 'Smith John' as
    well as 'John Smith' with exact_phrase=False.

    Args:
        query: Name or phrase to find, e.g. 'mary walsh'.
        start_date / end_date: 'YYYY-MM-DD' or 'YYYY'.
        state: Full US state name, e.g. 'Missouri'.
        exact_phrase: True = exact phrase; False = all words anywhere on the page.
        count: Results per page (1-100). page: 1-based page number.

    Searches often take 10-30 s at the Library of Congress; a narrow date range
    and a state make them faster and less likely to time out.

    Returns {'total', 'page', 'pages', 'results': [{title, newspaper, date,
    location, ocr_excerpt, page_url, image_url}]}. ocr_excerpt is the start of
    the page's OCR text and often doesn't contain the match — open page_url
    to read the page. To keep a page, pass its image_url to
    tree_attach_resource_from_url.
    """
    return newspapers.search(
        query,
        start_date=start_date,
        end_date=end_date,
        state=state,
        phrase=exact_phrase,
        count=count,
        page=page,
    )


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
    external_ids: dict[str, str] | None = None,
    tree: str = "default",
) -> dict:
    """Add a person to your tree. Link them to relatives afterwards with tree_link.

    Args:
        sex: 'M', 'F' or 'U'.
        birth_date / death_date: GEDCOM date phrases, e.g. '12 MAR 1850',
            'ABT 1850', 'BEF 1900', 'BET 1850 AND 1855'.
        external_ids: Ids of this person elsewhere, e.g. {"wikitree": "Clemens-1",
            "familysearch": "KWQ7-ABC"}. Imports match people on these.
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
        for system, ext_id in (external_ids or {}).items():
            t.set_external_id(person, system, ext_id)
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
    external_ids: dict[str, str] | None = None,
    tree: str = "default",
) -> dict:
    """Change a person's name, sex, living flag or external ids, or append a note.

    Only the arguments you pass are changed. Use tree_set_event for dates/places.

    Args:
        external_ids: e.g. {"wikitree": "Clemens-1"}; an empty string clears one.
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
        for system, ext_id in (external_ids or {}).items():
            t.set_external_id(p, system, ext_id)
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
def tree_import_wikitree(
    wikitree_id: str,
    generations: int = 4,
    overwrite: bool = False,
    tree: str = "default",
) -> dict:
    """Copy a WikiTree profile and its direct ancestors into your tree.

    People are matched on WikiTree id (and any other shared external id), so
    re-running is safe: existing people keep your local edits and only gain
    missing data, unless overwrite=True (WikiTree's names/events win; your
    notes and resources are kept).

    Args:
        wikitree_id: Root profile, e.g. 'Clemens-1'.
        generations: 1-10 (default 4). 1 = the profile and its parents.
    """
    profiles = wikitree.ancestors(wikitree_id, generations)
    if not profiles:
        raise ValueError(f"WikiTree returned no pedigree for {wikitree_id!r}")
    incoming = wikitree.ancestors_tree(profiles)
    with _edit(tree) as t:
        stats = t.merge_tree(incoming, overwrite=overwrite)
        root = t.find_by_external_id(wikitree.SYSTEM, wikitree_id)
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
    source: str | None = None,
    mode: str = "merge",
) -> dict:
    """Load a GEDCOM file (e.g. an Ancestry or RootsMagic export) into a tree.

    Provide either gedcom_text (the file contents) or stored_path (a file already
    in the tree's storage, e.g. 'uploads/ancestry.ged' — large files are best
    uploaded there directly).

    Args:
        source: Which app made the file, e.g. 'ancestry' or 'rootsmagic'. Set it
            for files from other apps: each person is keyed by that app's id for
            them (_UID, else the GEDCOM xref), so importing a newer export from
            the same app updates the same people instead of duplicating them.
            Not needed for a tree_export_gedcom file (edited elsewhere or not),
            which carries this tree's own ids.
        mode: 'merge' (default) adds new people and fills gaps in existing
            ones, keeping your edits — same rules as tree_import_wikitree.
            'replace' discards the current tree and loads the file as-is.

    People also match on external ids carried in the file (REFN/TYPE pairs,
    which tree_export_gedcom writes, and FamilySearch _FSFTID tags).
    """
    store = get_store()
    if stored_path:
        text = store.read_file(tree, stored_path).decode("utf-8-sig")
    elif gedcom_text:
        text = gedcom_text
    else:
        raise ValueError("Provide gedcom_text or stored_path")
    if mode not in ("merge", "replace"):
        raise ValueError("mode must be 'merge' or 'replace'")
    incoming = from_gedcom(text, name=tree, source=source or None)
    if mode == "replace":
        _, version = store.load(tree)
        for p in incoming.people.values():  # ids from a previous export are stale
            p.external_ids.pop(incoming.own_id_system(), None)
        store.save(incoming, version)
        return {
            "tree": tree,
            "people": len(incoming.people),
            "families": len(incoming.families),
        }
    with _edit(tree) as t:
        stats = t.merge_tree(incoming)
        totals = {"people": len(t.people), "families": len(t.families)}
    return {"tree": tree, **stats, **totals}


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
