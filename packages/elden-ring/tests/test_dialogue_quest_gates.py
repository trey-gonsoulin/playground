"""Talk-line quest gates (#335) and talk options choosing an ending (#334): the
new fields are mapped (strict index) and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# A 1.17 build's docs, trimmed.
_LINE = {
    "entity_type": "npc_dialogue",
    "name": "Dialogue 334030000",
    "patch_version": "1.17.1",
    "speakers": ["Nepheli Loux, Warrior"],
    "talk_contexts": ["talk"],
    "requires_flags": [4227],
    "requires_steps": [
        {"quest": "Nepheli Loux, Warrior", "phase_flag": 4227, "order": 3}
    ],
}
_OPTION = {
    "entity_type": "talk_option",
    "name": "Talk Option 26071001",
    "patch_version": "1.17.1",
    "npcs": ["Fractured Marika"],
    "sets_flags": [9401],
    "cutscene": "Cutscene 19000060",
    "cutscene_id": 19000060,
}
_SCENE = {
    "entity_type": "cutscene",
    "name": "Cutscene 19000060",
    "trigger_flags": [9401, 19001100, 19002100],
    "trigger_options": ["Talk Option 26071001"],
}


def _leaves(doc, props, path=""):
    for k, v in doc.items():
        assert k in props, path + k
        items = v if isinstance(v, list) else [v]
        for item in items:
            if isinstance(item, dict):
                _leaves(item, props[k]["properties"], path + k + ".")


def test_fields_mapped():
    for doc in (_LINE, _OPTION, _SCENE):
        _leaves(doc, _PROPS)
    assert _PROPS["requires_flags"] == {"type": "long"}
    assert _PROPS["sets_flags"] == {"type": "long"}
    assert _PROPS["trigger_options"] == {"type": "keyword"}
    assert set(_PROPS["requires_steps"]["properties"]) >= {
        "quest",
        "phase_flag",
        "order",
    }


def test_fields_documented():
    for field in ("requires_flags", "requires_steps", "sets_flags", "trigger_options"):
        assert field in _FIELD_NOTES, field
    assert "npc_dialogue" in _FIELD_NOTES["requires_flags"]
    assert "Nepheli Loux" in _FIELD_NOTES["requires_steps"]
    assert "9401" in _FIELD_NOTES["sets_flags"]
    assert "talk_option" in _FIELD_NOTES["cutscene"]
    assert "talk_option" in _FIELD_NOTES["cutscene_id"]
    # each ending keeps its own choice flag (#334)
    assert "9403 Blessing of Despair" in _FIELD_NOTES["trigger_flags"]
