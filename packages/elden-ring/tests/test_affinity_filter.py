"""Unit tests for collapse_variants (#111, formerly collapse_affinity #21) and the
include_variants family lookup. No OpenSearch needed."""

from elden_ring import _client
from elden_ring._client import _variant_filter


def test_variant_filter_off_adds_nothing():
    assert _variant_filter(False) == []


def test_variant_filter_drops_docs_naming_a_base():
    # Excluded = has a base_item (affinity, rank, flask +N, altered armor); bases and
    # docs outside a family pass through.
    assert _variant_filter(True) == [
        {"bool": {"must_not": [{"exists": {"field": "base_item"}}]}}
    ]


class _Client:
    def __init__(self, hits):
        self.hits, self.bodies = hits, []

    def search(self, index, body):
        self.bodies.append(body)
        return {"hits": {"hits": [{"_source": h} for h in self.hits]}}


def _doc(name, **kw):
    return {"entity_type": "weapon", "name": name, "patch_version": "1.17.0", **kw}


def test_family_docs_from_a_variant_query_the_base_and_drop_self():
    heavy = _doc("Heavy Halberd", base_item="Halberd", ar_inputs={"x": 1})
    client = _Client([_doc("Halberd"), heavy, _doc("Keen Halberd", ar_inputs={})])
    out = _client._family_docs(client, heavy)
    assert [d["name"] for d in out] == ["Halberd", "Keen Halberd"]
    assert all("ar_inputs" not in d for d in out)  # internal fields stripped
    q = client.bodies[0]["query"]["bool"]
    assert {"term": {"patch_version": "1.17.0"}} in q["filter"]
    assert {"term": {"entity_type": "weapon"}} in q["filter"]
    assert q["should"] == [
        {"term": {"base_item": "Halberd"}},
        {"term": {"name.keyword": "Halberd"}},
    ]


def test_family_docs_from_a_base_use_its_own_name():
    client = _Client([])
    assert _client._family_docs(client, _doc("Halberd")) == []
    assert {"term": {"base_item": "Halberd"}} in client.bodies[0]["query"]["bool"][
        "should"
    ]


def test_get_entity_include_variants(monkeypatch):
    halberd = _doc("Halberd")
    monkeypatch.setattr(_client, "_resolve_entity_doc", lambda *a: (halberd, False))
    monkeypatch.setattr(_client, "_family_docs", lambda c, d: [_doc("Heavy Halberd")])
    assert "variant_docs" not in _client.get_entity(None, "Halberd")
    out = _client.get_entity(None, "Halberd", include_variants=True)
    assert [d["name"] for d in out["variant_docs"]] == ["Heavy Halberd"]
