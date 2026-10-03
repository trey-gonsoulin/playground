"""World-state gate flags described by what sets them (#228): every leaf the builder
emits is in the strict mapping and the notes document it. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# Shapes as the builder emits them (1.17).
_FESTIVAL = [
    {
        "kind": "any_of",
        "conditions": [
            {"kind": "talk", "flag": 1034499224, "npcs": ["Smithing Master Iji"]},
            {"kind": "talk", "flag": 1044369223, "npcs": ["Sorceress Sellen"]},
            {
                "kind": "quest_phase",
                "flag": 3063,
                "npc": "Heartbroken Maiden",
                "quest": "Heartbroken Maiden (3060–3079)",
            },
        ],
    },
    {"kind": "flag", "flag": 9411, "set_at": ["Caelid"], "negated": True},
    {
        "kind": "boss_defeated",
        "flag": 9412,
        "bosses": ["Starscourge Radahn"],
        "negated": True,
    },
]
_KEY = [
    {"kind": "flag", "flag": 1033462613},
    {"kind": "item_held", "items": ["Imbued Sword Key"]},
    {"kind": "untracked_wait"},
]


def _mapped(props: dict, doc: dict) -> None:
    for k, v in doc.items():
        assert k in props, k
        if isinstance(v, dict):
            _mapped(props[k]["properties"], v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            for x in v:
                _mapped(props[k]["properties"], x)


def test_set_when_fields_mapped():
    _mapped(
        _PROPS,
        {
            "gate_set_when": _KEY,
            "npc_summons": [
                {
                    "npc": "Castellan Jerren",
                    "requires_flag": 1252382890,
                    "requires_set_when": [
                        *_FESTIVAL[2:],
                        {"kind": "untracked_wait"},
                        {
                            "kind": "character",
                            "state": "special_effect",
                            "entity_ids": [1052380800],
                        },
                    ],
                }
            ],
            "hostile_signs": [
                {
                    "kind": "invasion",
                    "requires_flag": 1039529206,
                    "requires_set_when": _FESTIVAL,
                }
            ],
            "trigger_set_when": [
                {"flag": 9410, "when": _FESTIVAL},
                {
                    "flag": 19001100,
                    "when": [{"kind": "character", "state": "dead", "npc": "x"}],
                },
                # #232: no event sets it; the talk script that does
                {
                    "flag": 1051362702,
                    "when": [
                        {
                            "kind": "talk",
                            "flag": 1051362702,
                            "npcs": ["Castellan Jerren"],
                        }
                    ],
                },
                {"flag": 9000, "when": [{"kind": "talk", "flag": 9000}]},
            ],
        },
    )
    when = _PROPS["trigger_set_when"]["properties"]["when"]["properties"]
    assert when["conditions"]["properties"]["flag"]["type"] == "long"
    assert _PROPS["gate_set_when"]["properties"]["negated"]["type"] == "boolean"


def test_set_when_documented():
    assert "Imbued Sword Key" in _FIELD_NOTES["gate_set_when"]
    assert "unresolved" in _FIELD_NOTES["gate_set_when"].lower()
    assert "gate_set_when" in _FIELD_NOTES["gate_flag"]
    assert "requires_set_when" in _FIELD_NOTES["npc_summons"]
    assert "requires_set_when" in _FIELD_NOTES["hostile_signs"]
    assert "{flag, when}" in _FIELD_NOTES["trigger_set_when"]
    # guards beside an undecoded wait aren't presented as enough (#228)
    assert "untracked_wait" in _FIELD_NOTES["gate_set_when"]
    assert "quest-only" in _FIELD_NOTES["npc_summons"]
    # a flag only a talk script or pickup sets (#232)
    assert "item_pickup" in _FIELD_NOTES["gate_set_when"]
    assert "Castellan Jerren" in _FIELD_NOTES["trigger_set_when"]
