"""Coffin placements (#395) and world-doc extras (#315): location items /
enemies / landmark graces and the item-use warp. Mapped and documented; no
OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_new_fields_mapped():
    for key in ("items", "enemies", "graces", "item"):
        assert _PROPS[key] == {"type": "keyword"}, key
    # placements are returned, not searched: in_coffin needs no mapping
    assert _PROPS["placements"] == {"type": "object", "enabled": False}


def test_new_fields_documented():
    assert "in_coffin" in _FIELD_NOTES["placements"]
    # boss characters are listed in enemies too; talk objects are not
    # trap-chest warps are named by the chest's location
