"""Unit tests for get_entity's placements trim (#135). No OpenSearch needed."""

from elden_ring._client import trim_placements


def _pls(n):
    return [{"map": "m60_42_36_00", "lot_id": i, "gathering": True} for i in range(n)]


def test_short_placements_pass_through():
    doc = {"name": "Golden Seed", "placements": _pls(3), "maps": ["m60_42_36_00"]}
    assert trim_placements(doc, limit=3) is doc


def test_long_placements_become_a_total():
    doc = {"name": "Rowa Fruit", "placements": _pls(4), "maps": ["m60_42_36_00"]}
    out = trim_placements(doc, limit=3)
    assert "placements" not in out
    assert out["placements_total"] == 4
    assert out["maps"] == ["m60_42_36_00"]
    assert len(doc["placements"]) == 4  # the input doc is not mutated


def test_variant_docs_trimmed_too():
    doc = {
        "name": "Halberd",
        "placements": _pls(1),
        "variant_docs": [
            {"name": "Heavy Halberd", "placements": _pls(5)},
            {"name": "x"},
        ],
    }
    out = trim_placements(doc, limit=3)
    assert out["placements"] == _pls(1)
    assert out["variant_docs"][0]["placements_total"] == 5
    assert out["variant_docs"][1] == {"name": "x"}


def test_none_passes_through():
    assert trim_placements(None) is None
