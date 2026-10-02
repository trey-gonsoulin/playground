"""Quest-step links on summon signs, cutscenes and warps (#189): every leaf the
builder emits is in the strict mapping and the notes document it. No OpenSearch
needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# Shapes as the builder emits them (1.17).
_STEP = {"quest": "Moore (4380–4399)", "npc": "Moore", "phase_flag": 4385, "order": 1}
_LIFE = {"quest": "Patches", "life_state": "dead"}


def _mapped(props: dict, doc: dict) -> None:
    for k, v in doc.items():
        assert k in props, k
        if isinstance(v, dict):
            _mapped(props[k]["properties"], v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            for x in v:
                _mapped(props[k]["properties"], x)


def test_quest_link_fields_mapped():
    _mapped(
        _PROPS,
        {
            "npc_summons": [
                {
                    "npc": "Nepheli Loux, Warrior",
                    "npc_id": 1,
                    "sign": "npc_white",
                    "requires_flag": 10009709,
                    "requires_step": _STEP,
                }
            ],
            "hostile_signs": [
                {
                    "kind": "invasion",
                    "map": "m61_47_46_00",
                    "sign_type": 21,
                    "requires_flag": 2045429296,
                    "requires_step": {"quest": "Millicent"},
                }
            ],
            "trigger_steps": [_STEP, _LIFE],
            "cutscene": "Cutscene 12040000",
        },
    )
    assert _PROPS["trigger_steps"]["properties"]["phase_flag"]["type"] == "long"
    assert _PROPS["trigger_steps"]["properties"]["quest"]["type"] == "keyword"


def test_quest_links_documented():
    assert "requires_step" in _FIELD_NOTES["npc_summons"]
    assert "requires_step" in _FIELD_NOTES["hostile_signs"]
    assert "invasion_instances" in _FIELD_NOTES["hostile_signs"]
    assert "phase_flag" in _FIELD_NOTES["trigger_steps"]
    assert "quest (" in _FIELD_NOTES["trigger_kind"]
    assert "warp" in _FIELD_NOTES["cutscene"]
