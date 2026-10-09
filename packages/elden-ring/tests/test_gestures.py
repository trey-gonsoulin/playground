"""Gesture entity type (#306): the doc shapes are covered by the strict mapping and
the type is documented. No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# As the 1.17.1 build emits them.
_DOCS = [
    {
        "entity_type": "gesture",
        "name": "Bow",
        "patch_version": "1.17.1",
        "source": "GestureParam",
        "availability": None,
        "description": None,
        "menu_category": "Gesture",
        "tags": ["Gesture"],
        "sort_id": 990180,
        "name_ja": "一礼",
        "acquisition_types": ["starting_equipment"],
        "starting_classes": ["Vagabond", "Warrior"],
        "display_name": "Bow",
        "is_dlc": False,
    },
    {
        "entity_type": "gesture",
        "name": "Reverential Bow",
        "patch_version": "1.17.1",
        "source": "GestureParam",
        "availability": None,
        "description": None,
        "menu_category": "Gesture",
        "tags": ["Gesture"],
        "sort_id": 990230,
        "name_ja": "恭しい一礼",
        "acquisition_types": ["given_by_npc"],
        "given_by": ["Mad Tongue Alberich"],
        "acquisition_sources": ["Mad Tongue Alberich"],
        "display_name": "Reverential Bow",
        "is_dlc": False,
    },
    {
        "entity_type": "gesture",
        "name": "The Carian Oath",
        "patch_version": "1.17.1",
        "source": "GestureParam",
        "availability": "cut",
        "description": None,
        "menu_category": "Gesture",
        "tags": ["Gesture"],
        "sort_id": None,
        "name_ja": "カーリアの誓い",
        "display_name": "The Carian Oath",
        "is_dlc": False,
    },
]


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        props = spec.get("properties", {})
    return spec


def test_gesture_docs_fully_mapped():
    for doc in _DOCS:
        for path in _flatten(doc):
            assert _mapped(path) is not None, path


def test_gesture_type_documented():
    assert "gesture (#306)" in _FIELD_NOTES["entity_type"]
    assert "gesture doc (#306)" in _FIELD_NOTES["acquisition_types"]
    assert "gesture doc" in _FIELD_NOTES["given_by"]
    assert "gesture      —" in mcp_server.search_entities.__doc__
