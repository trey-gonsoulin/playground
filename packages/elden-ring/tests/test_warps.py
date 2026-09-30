"""Warp docs (#94): mapped and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_warp_fields_mapped():
    for side in ("from", "to"):
        end = _PROPS[side]["properties"]
        for key in ("map", "grace", "region", "parent_region", "location"):
            assert end[key] == {"type": "keyword"}, (side, key)
        assert end["entity_id"] == {"type": "long"}
        # positions are returned, not searched
        assert end["world_position"] == {"type": "object", "enabled": False}
        assert end["position"] == {"type": "object", "enabled": False}
    assert _PROPS["prompt"] == {"type": "keyword"}
    for key in ("gate_flag", "event_id", "cutscene_id"):
        assert _PROPS[key] == {"type": "long"}, key
    assert _PROPS["warps_to"] == {"type": "keyword"}
    assert _PROPS["warps_from"] == {"type": "keyword"}
    assert _PROPS["kind"] == {"type": "keyword"}


def test_warp_fields_documented():
    for key in (
        "from",
        "to",
        "prompt",
        "gate_flag",
        "event_id",
        "cutscene_id",
        "warps_to",
        "warps_from",
    ):
        assert "#94" in _FIELD_NOTES[key], key
    kinds = _FIELD_NOTES["kind"]
    for kind in ("waygate", "return_to_entrance", "evergaol", "cutscene", "scripted"):
        assert kind in kinds, kind
    assert "90005605" in _FIELD_NOTES["event_id"]
