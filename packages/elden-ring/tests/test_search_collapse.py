"""Unscoped search collapse (#442), per-(entity_type, name) keys (#443) and the
capped patterns union (#447). No OpenSearch needed."""

import load_data
from elden_ring import _client
from elden_ring._client import INDEX_MAPPING


def _doc(name, version, entity_type="weapon", **kw):
    return {"name": name, "entity_type": entity_type, "patch_version": version, **kw}


def _bulk_id(d):
    return f"{d['entity_type']}::{d['name']}::{d['patch_version']}"


def _key(d):
    return f"{d.get('entity_type')}::{d.get('name')}"


def _hits(docs, score=1.0):
    """Collapsed hits: the collapse puts each group's entity_key in hit["fields"]."""
    return [
        {"_score": score, "_source": d, "fields": {"entity_key": [_key(d)]}}
        for d in docs
    ]


def _collapsed(docs, scores=None):
    """A collapsed search response, one hit per doc."""
    hits = _hits(docs)
    for h, sc in zip(hits, scores or [], strict=False):
        h["_score"] = sc
    return {
        "hits": {"hits": hits},
        "aggregations": {"distinct_entities": {"value": len(docs)}},
    }


class _Recorder:
    def __init__(self, resp):
        self.resp = resp
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        return self.resp


class _SearchClient:
    """search(): the text query returns `matched`; the follow-up newest-patch query
    (no text clause, a terms filter on entity_key) returns the newest doc per key
    among `corpus`."""

    def __init__(self, matched, corpus, scores=None):
        self.matched, self.corpus, self.scores = matched, corpus, scores
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        terms = [f["terms"] for f in body["query"]["bool"]["filter"] if "terms" in f]
        if not terms:
            return _collapsed(self.matched, self.scores)
        newest = {}
        for d in sorted(self.corpus, key=lambda d: d["patch_version"], reverse=True):
            if _key(d) in terms[0]["entity_key"]:
                newest.setdefault(_key(d), d)
        src = body.get("_source")
        docs = [
            {k: v for k, v in d.items() if src is None or k in src}
            for d in newest.values()
        ]
        hits = _hits(docs)
        for h, d in zip(hits, newest.values(), strict=True):
            h["fields"] = {"entity_key": [_key(d)]}
        return {"hits": {"hits": hits}}


def _unscoped(monkeypatch):
    monkeypatch.setattr(_client, "_request_version", lambda c, v: v)


def test_entity_key_mapped_and_noted():
    assert INDEX_MAPPING["mappings"]["properties"]["entity_key"] == {"type": "keyword"}
    assert "entity_key" in _client._FIELD_NOTES


def test_bulk_actions_stamp_entity_key():
    (a,) = load_data._to_bulk_actions([_doc("Lightning Spear", "1.17.0", "spell")])
    assert a["_source"]["entity_key"] == "spell::Lightning Spear"
    assert a["_id"] == "spell::Lightning Spear::1.17.0"


def test_search_returns_newest_doc_when_only_old_text_matches(monkeypatch):
    # #442 acceptance: only the 1.02 text matches the query, so the text query
    # returns only the 1.02 doc; the result is still the newest doc, with the
    # group's score.
    _unscoped(monkeypatch)
    old = _doc("Talisman", "1.02", "item", description="old text")
    new = _doc("Talisman", "1.10.0", "item", description="new text", effect="x")
    client = _SearchClient([old], [old, new], scores=[7.5])
    (hit,) = _client.search(client, "old text", is_dlc=False)["results"]
    assert hit == {"score": 7.5, **new}
    first, follow = client.bodies
    assert first["sort"][0] == {"_score": "desc"}
    assert first["collapse"] == {"field": "entity_key"}  # no inner_hits
    assert follow["size"] == 1
    assert follow["sort"] == [{"patch_version": "desc"}]
    assert follow["collapse"] == {"field": "entity_key"}
    assert "must" not in follow["query"]["bool"]
    # Same filters as the text query, plus the returned keys.
    assert follow["query"]["bool"]["filter"] == [
        *first["query"]["bool"]["filter"],
        {"terms": {"entity_key": ["item::Talisman"]}},
    ]


def test_search_newest_takes_include_fields(monkeypatch):
    _unscoped(monkeypatch)
    old = _doc("x", "1.02", "item", description="a")
    new = _doc("x", "1.17.0", "item", description="b")
    client = _SearchClient([old], [old, new])
    out = _client.search(client, "x", include_fields=["name", "patch_version"])
    assert out == {"results": [{"score": 1.0, "name": "x", "patch_version": "1.17.0"}]}
    assert client.bodies[1]["_source"] == ["name", "patch_version"]


def test_search_keeps_matched_doc_when_newest_missing(monkeypatch):
    _unscoped(monkeypatch)
    old = _doc("x", "1.02", "item")
    client = _SearchClient([old], [])
    assert _client.search(client, "x") == {"results": [{"score": 1.0, **old}]}


def test_search_same_name_two_types_are_two_results(monkeypatch):
    _unscoped(monkeypatch)
    spell = _doc("Lightning Spear", "1.17.0", "spell")
    weapon = _doc("Lightning Spear", "1.17.0", "weapon")
    client = _SearchClient([spell, weapon], [spell, weapon], scores=[3.0, 2.0])
    out = _client.search(client, "Lightning Spear")["results"]
    assert [d["entity_type"] for d in out] == ["spell", "weapon"]
    assert [d["score"] for d in out] == [3.0, 2.0]


def test_search_count_counts_entity_keys(monkeypatch):
    _unscoped(monkeypatch)
    client = _Recorder(_collapsed([{}, {}]))
    assert _client.search(client, "x", count_only=True) == {"total": 2}
    assert len(client.bodies) == 1
    agg = client.bodies[0]["aggs"]["distinct_entities"]["cardinality"]
    assert agg["field"] == "entity_key"


def test_search_scoped_has_no_collapse(monkeypatch):
    _unscoped(monkeypatch)
    client = _Recorder({"hits": {"hits": [{"_score": 1.0, "_source": {"name": "x"}}]}})
    assert _client.search(client, "x", patch_version="1.17.0") == {
        "results": [{"score": 1.0, "name": "x"}]
    }
    assert "collapse" not in client.bodies[0]


def test_search_literal_collapse_has_no_inner_hits(monkeypatch):
    # The patch_version-first sort already makes each group's top hit its newest
    # matching patch; inner_hits would cost one extra search per group.
    _unscoped(monkeypatch)
    new = _doc("A", "1.17.0")
    client = _Recorder(_collapsed([new]))
    out = _client._search_literal(client, pattern="A", include_fields=["name"])
    assert out == {"total": 1, "results": [new]}
    (body,) = client.bodies
    assert body["sort"][0] == {"patch_version": "desc"}
    assert body["collapse"] == {"field": "entity_key"}
    assert body["_source"] == ["name"]
    assert body["aggs"]["distinct_entities"]["cardinality"]["field"] == "entity_key"


def test_newest_doc_breaks_type_ties_on_entity_type():
    client = _Recorder({"hits": {"hits": []}})
    _client._newest_doc(client, {"name.keyword": "Golden Vow"}, None)
    assert client.bodies[0]["sort"][1:] == [
        {"patch_version": "desc"},
        {"entity_type": "asc"},
    ]


def test_untyped_name_lookup_ranks_keepsake_docs_last():
    # A keepsake doc shares its granted item's name (#475); "keepsake" sorts before
    # key_item / tool / spirit_ash / upgrade_material, so it must be demoted first.
    client = _Recorder({"hits": {"hits": []}})
    _client._newest_doc(client, {"name.keyword": "Golden Seed"}, None)
    demote = client.bodies[0]["sort"][0]["_script"]
    assert demote["order"] == "asc"
    assert "keepsake" in demote["script"]["params"]["demoted"]
    # An explicit entity_type needs no demotion.
    _client._newest_doc(client, {"name.keyword": "Golden Seed"}, "keepsake")
    assert client.bodies[1]["sort"] == [
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
        src = body["_source"]
        keyed = [{k: v for k, v in d.items() if k in src} for d in docs]
        return {
            "hits": {"hits": [{"_source": k} for k in keyed]},
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
    assert all(b["_source"] == _client._UNION_KEY_FIELDS for b in subs)
    assert all(b["collapse"] == {"field": "entity_key"} for b in subs)
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
    monkeypatch.setattr(_client, "_request_version", lambda c, v: v)
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
