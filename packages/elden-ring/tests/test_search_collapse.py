"""Unscoped search collapse (#442), per-(entity_type, name) keys (#443) and the
capped patterns union (#447). No OpenSearch needed."""

import sys
from pathlib import Path

from elden_ring import _client
from elden_ring._client import INDEX_MAPPING

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import load_data  # noqa: E402


def _doc(name, version, entity_type="weapon", **kw):
    return {"name": name, "entity_type": entity_type, "patch_version": version, **kw}


def _bulk_id(d):
    return f"{d['entity_type']}::{d['name']}::{d['patch_version']}"


def _collapsed(groups):
    """A collapsed search response: each group is (top hit, newest hit, score)."""
    return {
        "hits": {
            "hits": [
                {
                    "_score": score,
                    "_source": top,
                    "inner_hits": {"latest": {"hits": {"hits": [{"_source": newest}]}}},
                }
                for top, newest, score in groups
            ]
        },
        "aggregations": {"distinct_entities": {"value": len(groups)}},
    }


class _Recorder:
    def __init__(self, resp):
        self.resp = resp
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        return self.resp


def _unscoped(monkeypatch):
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: v)


def test_entity_key_mapped_and_noted():
    assert INDEX_MAPPING["mappings"]["properties"]["entity_key"] == {"type": "keyword"}
    assert "entity_key" in _client._FIELD_NOTES


def test_bulk_actions_stamp_entity_key():
    (a,) = load_data._to_bulk_actions([_doc("Lightning Spear", "1.17.0", "spell")])
    assert a["_source"]["entity_key"] == "spell::Lightning Spear"
    assert a["_id"] == "spell::Lightning Spear::1.17.0"


def test_search_returns_newest_doc_with_group_score(monkeypatch):
    # Only the 1.02 text matched the extra term, so the 1.02 doc tops the group;
    # the result is still the newest matching patch, with the group's score.
    _unscoped(monkeypatch)
    old = _doc("Talisman", "1.02", "item", description="old text")
    new = _doc("Talisman", "1.10.0", "item", description="new text", effect="x")
    client = _Recorder(_collapsed([(old, new, 7.5)]))
    (hit,) = _client.search(client, "old text")
    assert hit == {"score": 7.5, **new}
    body = client.bodies[0]
    assert body["sort"][0] == {"_score": "desc"}
    assert body["collapse"]["field"] == "entity_key"
    inner = body["collapse"]["inner_hits"]
    assert inner["name"] == "latest" and inner["size"] == 1
    assert inner["sort"] == [{"patch_version": "desc"}]
    assert body["_source"] is False


def test_search_inner_hits_take_include_fields(monkeypatch):
    _unscoped(monkeypatch)
    client = _Recorder(_collapsed([({}, {"name": "x"}, 1.0)]))
    _client.search(client, "x", include_fields=["name"])
    assert client.bodies[0]["collapse"]["inner_hits"]["_source"] == ["name"]


def test_search_same_name_two_types_are_two_results(monkeypatch):
    _unscoped(monkeypatch)
    spell = _doc("Lightning Spear", "1.17.0", "spell")
    weapon = _doc("Lightning Spear", "1.17.0", "weapon")
    client = _Recorder(_collapsed([(spell, spell, 3.0), (weapon, weapon, 2.0)]))
    out = _client.search(client, "Lightning Spear")
    assert [d["entity_type"] for d in out] == ["spell", "weapon"]


def test_search_count_counts_entity_keys(monkeypatch):
    _unscoped(monkeypatch)
    client = _Recorder(_collapsed([({}, {}, 1.0), ({}, {}, 1.0)]))
    assert _client.search(client, "x", count_only=True) == {"total": 2}
    agg = client.bodies[0]["aggs"]["distinct_entities"]["cardinality"]
    assert agg["field"] == "entity_key"


def test_search_scoped_has_no_collapse(monkeypatch):
    _unscoped(monkeypatch)
    client = _Recorder({"hits": {"hits": [{"_score": 1.0, "_source": {"name": "x"}}]}})
    assert _client.search(client, "x", patch_version="1.17.0") == [
        {"score": 1.0, "name": "x"}
    ]
    assert "collapse" not in client.bodies[0]


def test_search_literal_collapse_uses_latest_inner_hit(monkeypatch):
    _unscoped(monkeypatch)
    old, new = _doc("A", "1.02"), _doc("A", "1.17.0")
    client = _Recorder(_collapsed([(old, new, 1.0)]))
    out = _client._search_literal(client, pattern="A")
    assert out == {"total": 1, "results": [new]}
    body = client.bodies[0]
    assert body["collapse"]["field"] == "entity_key"
    assert body["collapse"]["inner_hits"]["name"] == "latest"
    assert body["aggs"]["distinct_entities"]["cardinality"]["field"] == "entity_key"


def test_newest_doc_breaks_type_ties_on_entity_type():
    client = _Recorder({"hits": {"hits": []}})
    _client._newest_doc(client, {"name.keyword": "Golden Vow"}, None)
    assert client.bodies[0]["sort"] == [
        {"patch_version": "desc"},
        {"entity_type": "asc"},
    ]


class _UnionClient:
    """Answers pattern sub-queries from {pattern: docs} and ids queries from docs."""

    def __init__(self, by_pattern, totals=None):
        self.by_pattern = by_pattern
        self.totals = totals or {}
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        q = body["query"]
        if "ids" in q:
            want = set(q["ids"]["values"])
            docs = [d for ds in self.by_pattern.values() for d in ds]
            hits = []
            for d in docs:
                if _bulk_id(d) in want and _bulk_id(d) not in {h["_id"] for h in hits}:
                    src = d
                    if body.get("_source") is not None:
                        src = {k: v for k, v in d.items() if k in body["_source"]}
                    hits.append({"_id": _bulk_id(d), "_source": src})
            return {"hits": {"hits": hits}}
        pattern = q["bool"]["must"][0]["multi_match"]["query"]
        docs = self.by_pattern.get(pattern, [])
        src = body["collapse"]["inner_hits"]["_source"]
        keyed = [{k: v for k, v in d.items() if k in src} for d in docs]
        return {
            "hits": {
                "hits": [
                    {"inner_hits": {"latest": {"hits": {"hits": [{"_source": k}]}}}}
                    for k in keyed
                ]
            },
            "aggregations": {
                "distinct_entities": {"value": self.totals.get(pattern, len(docs))}
            },
        }


def _full(name, entity_type="weapon"):
    return _doc(name, "1.17.0", entity_type, description=f"{name} text " * 50)


def test_union_dedupes_on_type_and_name_and_fetches_full_docs():
    spell, weapon, other = (
        _full("Lightning Spear", "spell"),
        _full("Lightning Spear"),
        _full("Spear"),
    )
    client = _UnionClient({"a": [spell, other], "b": [weapon, other]})
    out = _client._search_literal(client, patterns=["a", "b"])
    assert out == {"total": 3, "results": [spell, other, weapon]}
    subs = [b for b in client.bodies if "ids" not in b["query"]]
    assert all(
        b["collapse"]["inner_hits"]["_source"] == _client._UNION_KEY_FIELDS
        for b in subs
    )
    assert all(b["size"] == _client._UNION_FETCH_SIZE for b in subs)
    (fetch,) = [b for b in client.bodies if "ids" in b["query"]]
    assert fetch["query"]["ids"]["values"] == [
        _bulk_id(d) for d in (spell, other, weapon)
    ]
    assert "_source" not in fetch


def test_union_fetches_only_first_limit_keys_with_include_fields():
    docs = [_full(f"W{i}") for i in range(5)]
    client = _UnionClient({"a": docs[:3], "b": docs[2:]})
    out = _client._search_literal(
        client, patterns=["a", "b"], limit=2, include_fields=["sort_id", "description"]
    )
    assert out["total"] == 5 and "total_is_lower_bound" not in out
    assert out["results"] == [{"description": d["description"]} for d in docs[:2]]
    (fetch,) = [b for b in client.bodies if "ids" in b["query"]]
    assert fetch["size"] == 2
    assert fetch["_source"] == ["sort_id", "description"]


def test_union_count_only_skips_full_fetch():
    client = _UnionClient({"a": [_full("A")], "b": [_full("B")]})
    assert _client._search_literal(client, patterns=["a", "b"], count_only=True) == {
        "total": 2
    }
    assert not any("ids" in b["query"] for b in client.bodies)


def test_union_flags_lower_bound_when_a_pattern_overflows():
    client = _UnionClient({"a": [_full("A")], "b": [_full("B")]}, totals={"a": 12000})
    out = _client._search_literal(client, patterns=["a", "b"], count_only=True)
    assert out == {"total": 2, "total_is_lower_bound": True}


def test_union_dedupes_patterns():
    client = _UnionClient({"a": [_full("A")]})
    _client._search_literal(client, pattern="a", patterns=["a", "a"], count_only=True)
    assert len(client.bodies) == 1


def test_search_literal_caps_distinct_patterns(monkeypatch):
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: v)
    many = [f"p{i}" for i in range(_client._MAX_PATTERNS + 1)]
    out = _client.search_literal(None, patterns=many)
    assert "error" in out and str(_client._MAX_PATTERNS) in out["error"]
    # Duplicates don't count toward the cap.
    seen = {}

    def fake(client, **kw):
        seen.update(kw)
        return {"total": 1, "results": []}

    monkeypatch.setattr(_client, "_search_literal", fake)
    out = _client.search_literal(None, patterns=["x"] * 50)
    assert "error" not in out and seen["patterns"] == ["x"] * 50
