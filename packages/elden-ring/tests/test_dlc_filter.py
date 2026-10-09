"""Unit tests for the is_dlc field and search filter (#297). No OpenSearch needed."""

from elden_ring import _client
from elden_ring._client import INDEX_MAPPING, _dlc_filter


def test_dlc_filter_none_adds_nothing():
    assert _dlc_filter(None) == []


def test_dlc_filter_true_and_false_are_terms():
    assert _dlc_filter(True) == [{"term": {"is_dlc": True}}]
    assert _dlc_filter(False) == [{"term": {"is_dlc": False}}]


def test_is_dlc_mapped_boolean_with_field_note():
    assert INDEX_MAPPING["mappings"]["properties"]["is_dlc"] == {"type": "boolean"}
    assert "is_dlc" in _client._FIELD_NOTES


class _Client:
    def __init__(self):
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        return {"hits": {"hits": [{"_score": 1.0, "_source": {"name": "x"}}]}}


def test_search_passes_is_dlc(monkeypatch):
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: "1.17.1")
    client = _Client()
    _client.search(client, "sword", is_dlc=True)
    assert {"term": {"is_dlc": True}} in client.bodies[0]["query"]["bool"]["filter"]
    _client.search(client, "sword")
    assert not any(
        "is_dlc" in f.get("term", {})
        for f in client.bodies[1]["query"]["bool"]["filter"]
    )


def test_search_literal_passes_is_dlc(monkeypatch):
    monkeypatch.setattr(_client, "_canonical_version", lambda c, v: "1.17.1")
    seen = {}

    def fake(client, **kw):
        seen.update(kw)
        return {"total": 1, "results": []}

    monkeypatch.setattr(_client, "_search_literal", fake)
    _client.search_literal(None, pattern="x", is_dlc=False)
    assert seen["is_dlc"] is False
