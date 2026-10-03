"""WikiTree client + mapping. Fixtures mirror live api.wikitree.com responses
(top-level array, numeric Father/Mother ids, DataStatus per field)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from genealogy import _wikitree as wikitree
from genealogy import mcp_server as srv


def _profile(num, wt_id, first, last, gender, father=0, mother=0, **extra):
    return {
        "Id": num,
        "Name": wt_id,
        "FirstName": first,
        "LastNameAtBirth": last,
        "LastNameCurrent": last,
        "Gender": gender,
        "IsLiving": 0,
        "Father": father,
        "Mother": mother,
        "DataStatus": {"BirthDate": "certain", "DeathDate": "guess"},
        **extra,
    }


SAM = _profile(
    5185,
    "Clemens-1",
    "Samuel",
    "Clemens",
    "Male",
    5186,
    5188,
    MiddleName="Langhorne",
    BirthDate="1835-11-30",
    DeathDate="1910-00-00",
    BirthLocation="Florida, Monroe, Missouri, United States",
)
JOHN = _profile(5186, "Clemens-2", "John", "Clemens", "Male", BirthDate="1798-08-11")
JANE = _profile(5188, "Lampton-1", "Jane", "Lampton", "Female")


@pytest.fixture
def api(monkeypatch):
    """Route WikiTree calls to canned responses; records each request's params."""
    calls: list[dict] = []
    responses = {
        "getPeople": [
            {
                "status": "",
                "resultByKey": {"Clemens-1": {"Id": 5185}},
                "people": {str(p["Id"]): p for p in (JOHN, JANE, SAM)},
            }
        ],
        "searchPerson": [
            {"status": 0, "matches": [SAM], "total": 7, "start": 0, "limit": 1}
        ],
        "getRelatives": [
            {
                "items": [
                    {
                        "key": "Clemens-1",
                        "person": {
                            **SAM,
                            "Parents": {"5186": JOHN, "5188": JANE},
                            "Spouses": {
                                "5256": {
                                    **_profile(
                                        5256, "Langdon-1", "Olivia", "Langdon", "Female"
                                    ),
                                    "marriage_date": "1870-02-02",
                                    "marriage_location": "Elmira, New York, USA",
                                }
                            },
                            "Children": {},
                        },
                    }
                ],
                "status": 0,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        params = {
            k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()
        }
        calls.append(params)
        return httpx.Response(200, json=responses[params["action"]])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(wikitree, "_http", lambda: client)
    return calls


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.delenv("TREE_BUCKET", raising=False)
    monkeypatch.setenv("TREE_DIR", str(tmp_path))


@pytest.mark.parametrize(
    ("date", "status", "expected"),
    [
        ("1835-11-30", "certain", "30 NOV 1835"),
        ("1850-00-00", None, "1850"),
        ("1850-03-00", None, "MAR 1850"),
        ("1850-00-00", "guess", "ABT 1850"),
        ("1900-05-01", "before", "BEF 1 MAY 1900"),
        ("0000-00-00", None, None),
        ("", None, None),
    ],
)
def test_to_gedcom_date(date, status, expected):
    assert wikitree.to_gedcom_date(date, status) == expected


def test_to_person_maps_names_dates_and_ids():
    p = wikitree.to_person(SAM, "I1")
    assert (p.given, p.surname, p.sex) == ("Samuel Langhorne", "Clemens", "M")
    assert p.event("BIRT").date == "30 NOV 1835"
    assert p.event("DEAT").date == "ABT 1910"  # DataStatus guess
    assert p.external_ids == {"wikitree": "Clemens-1"}
    assert p.citations[0].url == "https://www.wikitree.com/wiki/Clemens-1"


def test_search_and_get_person_tools(api):
    found = srv.wikitree_search(first_name="Samuel", last_name="Clemens", limit=500)
    assert found["total"] == 7
    assert found["results"][0]["wikitree_id"] == "Clemens-1"
    sent = api[-1]
    assert sent["FirstName"] == "Samuel" and sent["limit"] == "100"
    assert sent["appId"] == "genealogy-mcp" and "BirthDate" not in sent

    person = srv.wikitree_get_person("Clemens-1")
    assert {p["wikitree_id"] for p in person["parents"]} == {"Clemens-2", "Lampton-1"}
    assert person["spouses"][0]["marriage"] == "2 FEB 1870, Elmira, New York, USA"


def test_get_ancestors_names_parents(api):
    out = srv.wikitree_get_ancestors("Clemens-1", generations=2)
    sam = out["ancestors"][0]
    assert (sam["father"], sam["mother"]) == ("Clemens-2", "Lampton-1")


def test_import_is_idempotent_and_merges_with_existing_people(api):
    # Someone already in the tree (e.g. from a GEDCOM import) with the same id.
    existing = srv.tree_add_person(
        given="Jane",
        surname="Lampton",
        note="from grandma's bible",
        external_ids={"wikitree": "Lampton-1"},
    )["added"]["id"]

    first = srv.tree_import_wikitree("Clemens-1", generations=1)
    assert (first["created"], first["updated"]) == (2, 1)
    assert first["root"]["name"] == "Samuel Langhorne Clemens"
    again = srv.tree_import_wikitree("Clemens-1", generations=1)
    assert again["created"] == 0

    summary = srv.tree_summary()
    assert (summary["people"], summary["families"]) == (3, 1)
    jane = srv.tree_get_person(existing)
    assert jane["person"]["notes"] == ["from grandma's bible"]
    assert [c["name"] for c in jane["children"]] == ["Samuel Langhorne Clemens"]
