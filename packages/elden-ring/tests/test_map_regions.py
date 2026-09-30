"""Region map / landmark footprints (#80) and NPC-invasion instances (#96): the new
doc shapes are covered by the strict mapping and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        if spec.get("enabled") is False:  # stored, not indexed: covers its subtree
            return spec
        props = spec.get("properties", {})
    return spec


def _leaves(doc):
    for path, v in _flatten(doc).items():
        yield path
        for item in v if isinstance(v, list) else []:
            if isinstance(item, dict):
                yield from _flatten(item, f"{path}.")


def test_location_and_placement_shapes_fully_mapped():
    # Shapes as the 1.17 build emits them (Stormhill / Revenger's Shack / Istvan).
    docs = [
        {
            "entity_type": "location",
            "name": "Revenger's Shack",
            "area_scaling": [
                {
                    "speffect_id": 7120,
                    "placements": 8,
                    "hp": 4.844,
                    "stamina": 1.6,
                    "attack": 3.244,
                    "defense": 1.159,
                    "resistance": 2.21,
                }
            ],
            "footprint": [
                {
                    "shape": "box",
                    "map": "m60_33_44_00",
                    "world_position": {"x": 8465.93, "y": 252.62, "z": 11291.95},
                    "rotation_y": -24.04,
                    "width": 7.0,
                    "depth": 7.0,
                    "height": 10.0,
                }
            ],
        },
        {
            "entity_type": "location",
            "name": "Stormhill",
            "invasion_instances": [
                {"ceremony": 20, "hosts": ["Old Knight Istvan"], "invader_flag": 7602}
            ],
        },
        {
            "entity_type": "enemy",
            "name": "Old Knight Istvan",
            "placements": [
                {
                    "map": "m60_42_39_00",
                    "world_position": {"x": 10791.0, "y": 90.0, "z": 10108.0},
                    "entity_id": 1042390700,
                    "region": "Limgrave",
                    "area": "Stormhill",
                    "world_state": {
                        "kind": "npc_invasion",
                        "ceremony": 20,
                        "host": "Old Knight Istvan",
                        "invader_flag": 7602,
                    },
                }
            ],
            "locations": ["Castle Morne"],
        },
    ]
    for doc in docs:
        for path in _leaves(doc):
            assert _mapped(path) is not None, path


def test_footprint_and_placements_not_indexed():
    assert _PROPS["footprint"] == {"type": "object", "enabled": False}
    assert _PROPS["placements"]["enabled"] is False
    inst = _PROPS["invasion_instances"]["properties"]
    assert inst["hosts"] == {"type": "keyword"}
    assert inst["invader_flag"]["type"] == "long"


def test_notes_document_new_fields():
    pl = _FIELD_NOTES["placements"]
    for word in ("area", "subarea", "world_state", "npc_invasion", "landmark"):
        assert word in pl, word
    assert "footprint" in _FIELD_NOTES["area_scaling"]
    assert "rotation_y" in _FIELD_NOTES["footprint"]
    assert "invader_flag" in _FIELD_NOTES["invasion_instances"]
    assert "Castle Morne" in _FIELD_NOTES["locations"]
