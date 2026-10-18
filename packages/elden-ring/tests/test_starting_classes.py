"""Starting class entity type (#422): the doc shape is covered by the strict mapping
and the type is documented. No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING, _flatten

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# As the 1.17.0 build emits it.
_DOC = {
    "entity_type": "starting_class",
    "name": "Samurai",
    "name_ja": "侍",
    "patch_version": "1.17.0",
    "source": "CharaInitParam",
    "param_id": 3007,
    "description": "A capable fighter from the distant Land of\nReeds. Handy with "
    "katana and longbows.",
    "description_ja": "隔絶した異国たる葦の地の侍\n刀と長弓を用いる戦闘者",
    "level": 9,
    "attributes": {
        "vigor": 12,
        "mind": 11,
        "endurance": 13,
        "strength": 12,
        "dexterity": 15,
        "intelligence": 9,
        "faith": 8,
        "arcane": 8,
    },
    "flasks": {"crimson_tears": 3, "cerulean_tears": 1},
    "equipment": {
        "weapons": ["Uchigatana", "Longbow", "Red Thorn Roundshield"],
        "armor": ["Land of Reeds Helm", "Land of Reeds Armor (Altered)"],
        "ammo": ["Arrow", "Fire Arrow"],
    },
    "weapon_slots": [
        {"slot": "right_hand_1", "weapon": "Uchigatana", "upgrade_level": 0},
        {"slot": "left_hand_1", "weapon": "Longbow", "upgrade_level": 0},
    ],
    "ammo_slots": [
        {"slot": "arrow_1", "ammo": "Arrow", "quantity": 20},
        {"slot": "arrow_2", "ammo": "Fire Arrow", "quantity": 10},
    ],
    "items": ["Memory of Grace"],
    "gestures": ["Bow", "Warm Welcome", "Rest"],
    "is_dlc": False,
}


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        props = spec.get("properties", {})
    return spec


def test_starting_class_doc_fully_mapped():
    for path in _flatten(_DOC):
        assert _mapped(path) is not None, path
    assert _mapped("attributes.arcane") == {"type": "integer"}
    assert _mapped("ammo_slots.quantity") == {"type": "integer"}


def test_starting_class_type_documented():
    assert "starting_class" in _FIELD_NOTES["entity_type"]
    for field in ("level", "attributes", "flasks", "weapon_slots", "ammo_slots"):
        assert field in _FIELD_NOTES, field
    assert "starting_class —" in mcp_server.search_entities.__doc__
