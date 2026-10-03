import json
from urllib.parse import parse_qs

import httpx
import pytest

from genealogy import _familysearch as fs
from genealogy._models import Tree


def _gx(
    pid: str,
    given: str,
    surname: str,
    gender: str,
    number: str | None = None,
    birth=None,
):
    person = {
        "id": pid,
        "living": False,
        "gender": {"type": f"http://gedcomx.org/{gender}"},
        "names": [
            {
                "preferred": True,
                "nameForms": [
                    {
                        "fullText": f"{given} {surname}",
                        "parts": [
                            {"type": "http://gedcomx.org/Given", "value": given},
                            {"type": "http://gedcomx.org/Surname", "value": surname},
                        ],
                    }
                ],
            }
        ],
        "facts": [],
        "display": {"name": f"{given} {surname}", "gender": gender},
    }
    if birth:
        person["facts"].append(
            {
                "type": "http://gedcomx.org/Birth",
                "date": {"original": "about 1850", "formal": birth},
                "place": {"original": "Cork, Ireland"},
            }
        )
    if number:
        person["display"]["ascendancyNumber"] = number
    return person


@pytest.mark.parametrize(
    ("formal", "expected"),
    [
        ("+1850", "1850"),
        ("+1850-03", "MAR 1850"),
        ("+1850-03-07", "7 MAR 1850"),
        ("A+1850", "ABT 1850"),
        ("+1850/+1855", "BET 1850 AND 1855"),
        ("/+1900", "BEF 1900"),
        ("+1900/", "AFT 1900"),
        ("", None),
    ],
)
def test_formal_to_gedcom(formal, expected):
    assert fs.formal_to_gedcom(formal) == expected


def test_import_ancestry_builds_pedigree_and_is_idempotent():
    data = {
        "persons": [
            _gx("AAAA-111", "Ann", "Walsh", "Female", "1", birth="A+1890"),
            _gx("BBBB-222", "John", "Walsh", "Male", "2"),
            _gx("CCCC-333", "Mary", "Burke", "Female", "3"),
            _gx("DDDD-444", "Pat", "Walsh", "Male", "4"),
        ]
    }
    tree = Tree()
    stats = fs.import_ancestry(tree, data)
    assert stats == {"created": 4, "updated": 0, "parent_links": 2}

    ann = tree.find_by_external_id("familysearch", "AAAA-111")
    assert ann and ann.event("BIRT").date == "ABT 1890"
    parents = {p["name"] for p in tree.relatives(ann.id)["parents"]}
    assert parents == {"John Walsh", "Mary Burke"}

    ann.notes.append("my note")
    again = fs.import_ancestry(tree, data)
    assert again["created"] == 0 and again["updated"] == 4
    assert len(tree.people) == 4 and len(tree.families) == 2
    assert tree.find_by_external_id("familysearch", "AAAA-111").notes == ["my note"]


def test_reimport_completes_single_parent_family():
    ann, john, mary = (
        _gx("A-1", "Ann", "W", "Female", "1"),
        _gx("B-2", "John", "W", "Male", "2"),
        _gx("C-3", "Mary", "B", "Female", "3"),
    )
    tree = Tree()
    fs.import_ancestry(tree, {"persons": [ann, john]})
    # FamilySearch later gains the mother: the father-only family is completed.
    fs.import_ancestry(tree, {"persons": [ann, john, mary]})
    (fam,) = tree.families.values()
    assert len(fam.partner_ids) == 2


def test_add_parent_joins_single_parent_family():
    tree = Tree()
    fs.import_ancestry(tree, {"persons": [_gx("A-1", "Ann", "W", "Female", "1")]})
    tree.merge_people(
        [fs.gx_to_person(_gx(pid, "X", "Y", "Male")) for pid in ("B-2", "C-3")],
        fs.SYSTEM,
    )
    child, p1, p2 = ("I1", "I2", "I3")
    tree.add_parent(child, p1)
    tree.add_parent(child, p2)
    tree.add_parent(child, p2)
    (fam,) = tree.families.values()
    assert fam.partner_ids == [p1, p2] and fam.child_ids == [child]


def test_person_detail_relationships():
    doc = {
        "persons": [
            _gx("A-1", "Ann", "W", "Female"),
            _gx("B-2", "John", "W", "Male"),
            _gx("S-9", "Tom", "K", "Male"),
            _gx("K-5", "Kid", "K", "Female"),
        ],
        "relationships": [
            {
                "type": "http://gedcomx.org/Couple",
                "person1": {"resourceId": "S-9"},
                "person2": {"resourceId": "A-1"},
            }
        ],
        "childAndParentsRelationships": [
            {"parent1": {"resourceId": "B-2"}, "child": {"resourceId": "A-1"}},
            {
                "parent1": {"resourceId": "S-9"},
                "parent2": {"resourceId": "A-1"},
                "child": {"resourceId": "K-5"},
            },
        ],
    }
    d = fs.person_detail(doc, "A-1")
    assert [p["pid"] for p in d["parents"]] == ["B-2"]
    assert [p["pid"] for p in d["spouses"]] == ["S-9"]
    assert [p["pid"] for p in d["children"]] == ["K-5"]


class _MemTokens(fs.TokenStore):
    def __init__(self, token):
        self.token, self.saved = token, None

    def load(self):
        return self.token

    def save(self, token):
        self.saved = token
        return "mem"


def test_client_search_query_and_refresh(monkeypatch):
    monkeypatch.setenv("FAMILYSEARCH_CLIENT_ID", "app-key")
    seen = []

    refreshed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == fs.ENVIRONMENTS["integration"].token_url:
            refreshed.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "new"})
        seen.append(request)
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"results": 0, "entries": []})

    tokens = _MemTokens({"access_token": "old", "refresh_token": "r"})
    client = fs.FamilySearchClient(
        env=fs.ENVIRONMENTS["integration"],
        tokens=tokens,
        transport=httpx.MockTransport(handler),
    )
    out = client.search(givenName="Ann", surname="Walsh", birthLikeDate=None, count=500)

    assert out == {"results": 0, "entries": []}
    assert refreshed[0]["grant_type"] == ["refresh_token"]
    assert tokens.saved["access_token"] == "new"
    last = seen[-1]
    assert last.url.path == "/platform/tree/search"
    assert last.url.params["q"] == 'givenName:"Ann" surname:"Walsh"'
    assert last.url.params["count"] == "100"
    assert last.headers["Accept"] == fs.FS_ATOM


def test_client_picks_up_token_saved_elsewhere():
    """A 401 first re-reads the store (another container may have refreshed)."""
    tokens = _MemTokens({"access_token": "old"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer old":
            tokens.token = {"access_token": "rotated"}
            return httpx.Response(401)
        return httpx.Response(200, json={"users": [{"personId": "P-1"}]})

    client = fs.FamilySearchClient(
        env=fs.ENVIRONMENTS["integration"],
        tokens=tokens,
        transport=httpx.MockTransport(handler),
    )
    assert client.current_user() == {"personId": "P-1"}
    assert tokens.saved is None


def test_client_without_token_raises():
    client = fs.FamilySearchClient(
        env=fs.ENVIRONMENTS["integration"],
        tokens=_MemTokens(None),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with pytest.raises(fs.NotAuthenticated):
        client.current_user()


def test_token_file_store(tmp_path, monkeypatch):
    monkeypatch.delenv("FAMILYSEARCH_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FAMILYSEARCH_TOKEN_SSM_PATH", raising=False)
    monkeypatch.setenv("FAMILYSEARCH_TOKEN_FILE", str(tmp_path / "tok.json"))
    store = fs.TokenStore()
    assert store.load() is None
    store.save({"access_token": "abc"})
    assert json.loads((tmp_path / "tok.json").read_text())["access_token"] == "abc"
    assert oct((tmp_path / "tok.json").stat().st_mode)[-3:] == "600"


def test_client_follows_merged_person_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/OLD-1"):
            return httpx.Response(
                301, headers={"Location": "/platform/tree/persons/NEW-2"}
            )
        return httpx.Response(200, json={"persons": [{"id": "NEW-2"}]})

    client = fs.FamilySearchClient(
        env=fs.ENVIRONMENTS["integration"],
        tokens=_MemTokens({"access_token": "t"}),
        transport=httpx.MockTransport(handler),
    )
    assert client.person("OLD-1") == {"persons": [{"id": "NEW-2"}]}
