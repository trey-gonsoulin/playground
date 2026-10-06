"""Tutorial unlock flags and what turns them on (#202): every leaf the builder emits
on a tutorial game_text doc is in the strict mapping and the notes document it."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# Shapes as the builder emits them (1.17; long lists shortened).
_DOCS = [
    {
        "unlock_flag": 710520,  # Horseback Riding: the whistle or the event
        "unlock_set_when": [
            {
                "kind": "any_of",
                "conditions": [
                    {"kind": "item_acquired", "items": ["Spectral Steed Whistle"]},
                    {
                        "kind": "all_of",
                        "conditions": [
                            {"kind": "item_held", "items": ["Spectral Steed Whistle"]},
                            {"kind": "in_own_world"},
                            {"kind": "untracked_wait"},
                            {
                                "kind": "character",
                                "state": "special_effect",
                                "entity_ids": [10000],
                                "negated": True,
                            },
                        ],
                    },
                ],
            }
        ],
    },
    {
        "unlock_flag": 710640,  # Teardrop Scarabs
        "unlock_set_when": [
            {"kind": "enemy_killed", "npc_param_ids": [41900000, 41900050]}
        ],
    },
    {"unlock_flag": 710610, "unlock_set_when": [{"kind": "telescope_view"}]},
    {
        "unlock_flag": 710050,  # #259: holding none of 27 staffs and seals
        "unlock_set_when": [
            {
                "kind": "item_held",
                "items": ["Glintstone Staff", "Finger Seal"],
                "negated": True,
            },
            {"kind": "in_own_world"},
            {
                "kind": "in_region",
                "entity_id": 18002654,
                "map": "m18_00_00_00",
                "locations": ["Fringefolk Hero's Grave", "Stranded Graveyard"],
            },
        ],
    },
    {"unlock_flag": 710760},  # Multiplayer: nothing found sets it
]


def _mapped(props: dict, doc: dict) -> None:
    for k, v in doc.items():
        assert k in props, k
        if isinstance(v, dict):
            _mapped(props[k]["properties"], v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            for x in v:
                _mapped(props[k]["properties"], x)


def test_tutorial_unlock_fields_mapped():
    for doc in _DOCS:
        _mapped(_PROPS, doc)
    assert _PROPS["unlock_flag"]["type"] == "long"
    leaf = _PROPS["unlock_set_when"]["properties"]
    assert leaf["npc_param_ids"]["type"] == "integer"
    assert leaf["conditions"]["properties"]["items"]["type"] == "keyword"


def test_tutorial_unlock_documented():
    assert "UnlockEventFlagId" in _FIELD_NOTES["unlock_flag"]
    assert "site_of_grace" in _FIELD_NOTES["unlock_flag"]
    note = _FIELD_NOTES["unlock_set_when"]
    for word in (
        "gate_set_when",
        "item_acquired",
        "ItemGetTutorialFlagId",
        "enemy_killed",
        "npc_param_ids",
        "ChrDeadTutorialFlagId",
        "telescope_view",
        "enemy_group_reward",
        "spiritspring_region",
        "1.12",
        "holding none",
    ):
        assert word in note, word
    assert "holds none of them" in _FIELD_NOTES["steps.when"]
