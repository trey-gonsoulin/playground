"""Ending entity type (#425) and quest venues (#424): the doc shapes are covered by
the strict mapping and documented. No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# As the 1.17.0 build emits them (trigger_set_when trimmed).
_ORDER = {
    "entity_type": "ending",
    "name": "Age of Order",
    "patch_version": "1.17.0",
    "source": "EMEVD",
    "cutscene": "Cutscene 19000060",
    "cutscene_id": 19000060,
    "ending_flags": [9401],
    "boss": "Elden Beast",
    "trigger_options": ["Talk Option 26071001"],
    "npcs": ["Fractured Marika"],
    "choices": ["Use Mending Rune of Perfect Order"],
    "trigger_items": ["Mending Rune of Perfect Order"],
    "trigger_set_when": [
        {
            "flag": 9500,
            "when": [
                {
                    "kind": "item_pickup",
                    "flag": 9500,
                    "items": ["Mending Rune of Perfect Order"],
                }
            ],
        }
    ],
    "text_content": "Age of Order\nUse Mending Rune of Perfect Order\n"
    "Mending Rune of Perfect Order",
    "is_dlc": False,
}
_STARS = {
    "entity_type": "ending",
    "name": "Age of the Stars",
    "patch_version": "1.17.0",
    "source": "EMEVD",
    "cutscene": "Cutscene 19000020",
    "cutscene_id": 19000020,
    "ending_flags": [9404, 9405],
    "boss": "Elden Beast",
    "choices": ["Summon Ranni"],
    "trigger_steps": [
        {"quest": "Ranni the Witch", "phase_flag": 1034509406, "order": 1}
    ],
    "trigger_set_when": [
        {
            "flag": 9404,
            "when": [
                {"kind": "talk", "flag": 1034509406, "npcs": ["Ranni the Witch"]},
                {
                    "kind": "boss_defeated",
                    "flag": 19001100,
                    "bosses": ["Elden Beast"],
                },
                {
                    "kind": "action_button",
                    "action_button_id": 9610,
                    "prompt": "Summon Ranni",
                    "entity_id": 19001110,
                },
                {"kind": "in_own_world"},
            ],
        }
    ],
    "text_content": "Age of the Stars\nSummon Ranni",
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


def test_ending_docs_fully_mapped():
    for doc in (_ORDER, _STARS):
        for path in _flatten(doc):
            assert _mapped(path) is not None, path
    assert _mapped("ending_flags") == {"type": "long"}
    assert _mapped("choices") == {"type": "keyword"}
    # the cutscene doc's back-link
    assert _mapped("ending") == {"type": "keyword"}


def test_ending_type_documented():
    assert "ending (one doc per Erdtree ending, #425)" in _FIELD_NOTES["entity_type"]
    for field in ("ending", "ending_flags", "choices"):
        assert "#425" in _FIELD_NOTES[field], field
    for field in (
        "cutscene",
        "cutscene_id",
        "boss",
        "npcs",
        "trigger_options",
        "trigger_items",
        "trigger_steps",
        "trigger_set_when",
    ):
        assert "ending doc (#425)" in _FIELD_NOTES[field], field
    assert "ending       —" in mcp_server.search_entities.__doc__


def test_quest_placement_venues_documented():
    assert "(#424)" in _FIELD_NOTES["locations"]
    assert "Ancient Ruins of Rauh" in _FIELD_NOTES["locations"]
