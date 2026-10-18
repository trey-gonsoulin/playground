"""Literal-search field routing from the mapping (#446): JP subfields, disabled objects."""

from elden_ring import _client
from elden_ring._client import (
    _JP_BASE_FIELDS,
    _LITERAL_FIELDS,
    INDEX_MAPPING,
    _disabled_paths,
    _mapping_paths,
    _route_literal_fields,
    _unsearchable_fields,
)

_PATHS = _mapping_paths(INDEX_MAPPING["mappings"]["properties"])


def test_every_morph_field_routes():
    morph = {p for p, s in _PATHS.items() if "morph" in s.get("fields", {})}
    assert morph == set(_JP_BASE_FIELDS)
    assert "subtitles_ja" in morph
    for base in morph:
        for f in (base, base + ".ja", base + ".morph", base + ".lemma"):
            assert _route_literal_fields([f], True, False) == [base + ".lemma"]
            assert _route_literal_fields([f], False, True) == [base + ".morph"]
        assert {"lemma", "morph"} <= set(_PATHS[base]["fields"])


def test_subtitles_stay_opt_in():
    assert "subtitles" not in _LITERAL_FIELDS
    assert "subtitles_ja" not in _LITERAL_FIELDS


def test_disabled_paths_from_mapping():
    disabled = _disabled_paths()
    assert {"placements", "shop_listings", "upgrade_curve"} <= disabled
    assert "max_level.summon_stats.spell_attacks.status_buildup" in disabled
    assert all(_PATHS[p]["enabled"] is False for p in disabled)
    assert _unsearchable_fields(
        ["placements", "placements.map_id", "name", "placements_x"]
    ) == ["placements", "placements.map_id"]


def test_search_literal_rejects_disabled_field(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not query")

    monkeypatch.setattr(_client, "_search_literal", boom)
    out = _client.search_literal(None, pattern="x", fields=["name", "shop_listings"])
    assert out == {
        "error": "field(s) ['shop_listings'] are stored, not indexed; read it "
        "with get_entity"
    }


def test_disabled_field_valid_for_include_fields(monkeypatch):
    monkeypatch.setattr(_client, "_request_version", lambda c, v: v)
    seen = {}

    def fake(client, **kw):
        seen.update(kw)
        return {"total": 1, "results": []}

    monkeypatch.setattr(_client, "_search_literal", fake)
    out = _client.search_literal(None, pattern="x", include_fields=["shop_listings"])
    assert "error" not in out
    assert seen["include_fields"] == ["shop_listings"]
    # text_changed_between's precheck accepts the stored object too
    assert _client._precheck(None, ["upgrade_curve"]) is None


def test_coverage_warnings_skip_disabled():
    class _C:
        def search(self, index, body):
            names = body["aggs"]["has"]["filters"]["filters"]
            return {
                "aggregations": {
                    "has": {"buckets": {f: {"doc_count": 0} for f in names}}
                }
            }

    out = _client._field_coverage_warnings(_C(), ["placements", "sold_by"], "enemy")
    assert len(out) == 1 and out[0].startswith("sold_by:")


def test_describe_index_marks_unsearchable():
    class _C:
        def search(self, index, body):
            return {
                "aggregations": {"types": {"buckets": []}, "sources": {"buckets": []}}
            }

    fields = _client.describe_index(_C(), full=True)["fields"]
    assert list(fields) == list(_PATHS)
    flagged = {p for p, e in fields.items() if e.get("searchable") is False}
    assert flagged == _disabled_paths()
    assert fields["placements"]["type"] == "object"
    assert "searchable" not in fields["name"]
    assert fields["subtitles_ja"]["subfields"] == ["ja", "morph", "lemma"]
