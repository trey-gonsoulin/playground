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
    {
        "kind": "action_button",
        "action_button_id": 9523,
        "prompt": "Examine",
        "entity_id": 1033461611,
    },
]
# #231 decoded progress waits; #233 all_of inside any_of (one level)
_PROGRESS = [
    {
        "kind": "in_region",
        "entity_id": 1051362230,
        "map": "m60_51_36_00",
        "locations": ["Caelid"],
        "area": "Caelid",
        "subarea": "x",
        "landmark": "Redmane Castle",
        "negated": True,
    },
    {
        "kind": "flag_range",
        "first_flag": 76100,
        "last_flag": 76199,
        "range_state": "any_on",
    },
    {"kind": "in_own_world"},
    {"kind": "armor_equipped", "items": ["Head"]},
    {"kind": "armor_equipped", "item_id": 10000},
    # #234: the player within distance of an entity (Godskin Duo signs)
    {
        "kind": "near_entity",
        "entity_id": 13002721,
        "distance": 10.0,
        "map": "m13_00_00_00",
        "locations": ["Crumbling Farum Azula"],
        "negated": True,
    },
    # #245: not in multiplayer (the Divine Tower of Leyndell gate)
    {"kind": "multiplayer_state", "state": "multiplayer", "negated": True},
    {"kind": "untracked_wait"},
]
_ALTERNATIVES = [
    {
        "kind": "any_of",
        "conditions": [
            {
                "kind": "all_of",
                "conditions": [
                    {"kind": "talk", "flag": 1051362702, "npcs": ["Castellan Jerren"]},
                    *_PROGRESS,
                    {"kind": "character", "state": "dead", "entity_ids": [1]},
                    *_KEY,
                ],
            },
            {"kind": "character", "state": "dead", "npc": "x"},
            *_PROGRESS,
        ],
    }
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
                {"flag": 9411, "when": [*_PROGRESS, *_ALTERNATIVES]},
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
                # #237: common event 6910 sets its own slot flag for the host
                {"flag": 6910, "when": [{"kind": "in_own_world"}]},
                # #238: an event's conditions, or the talk script that sets it too
                {
                    "flag": 1042559207,
                    "when": [
                        {
                            "kind": "any_of",
                            "conditions": [
                                {
                                    "kind": "all_of",
                                    "conditions": [
                                        {"kind": "life_state", "flag": 4180},
                                        {"kind": "in_own_world"},
                                    ],
                                },
                                {"kind": "talk", "flag": 1042559207, "npcs": ["x"]},
                                {"kind": "item_pickup", "flag": 1, "items": ["y"]},
                            ],
                        }
                    ],
                },
            ],
        },
    )
    for alt in (_ALTERNATIVES, _PROGRESS):
        _mapped(_PROPS, {"gate_set_when": alt})
        _mapped(_PROPS, {"npc_summons": [{"requires_set_when": alt}]})
        _mapped(_PROPS, {"hostile_signs": [{"requires_set_when": alt}]})
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


def test_set_when_alternatives_and_own_slot_documented():
    """#238 talk / pickup alternatives, #237 own slot flags, #236 re-sets."""
    note = _FIELD_NOTES["gate_set_when"]
    assert "one more any_of alternative" in note
    assert "event id + slot" in note
    assert "already on" in note
    # an event's own slot flag is no longer named as never resolved
    assert "(an event's own slot flag, a computed flag)" not in note


def test_set_when_progress_and_all_of_documented():
    """#231 decoded waits and #233 all_of: every kind and leaf is in the notes."""
    note = _FIELD_NOTES["gate_set_when"]
    for word in (
        "action_button",
        "action_button_id",
        "prompt",
        "entity_id",
        "in_region",
        "map",
        "locations",
        "area / subarea / landmark",
        "flag_range",
        "first_flag",
        "last_flag",
        "all_on / all_off / any_on / any_off",
        "in_own_world",
        "armor_equipped",
        "item_id",
        "all_of",
        "untracked_wait",
    ):
        assert word in note, word
    # the #228 "differ by more than one condition" absence rule is gone
    assert "more than one condition" not in note
    assert "'Examine'" in note
    assert "with 9410 already on" in _FIELD_NOTES["trigger_set_when"]


def test_set_when_or_groups_and_paths_documented():
    """#234 near_entity and OR groups, #235 per-path alternatives, #242 the
    player's character type."""
    note = _FIELD_NOTES["gate_set_when"]
    for word in ("near_entity", "distance", "only some of the event's", "re-run"):
        assert word in note, word
    assert "character type is Alive" in note
    # distances and either-of waits are decoded now, no longer untracked examples
    assert "a distance, either of several regions" not in note
    assert _PROPS["gate_set_when"]["properties"]["distance"]["type"] == "float"
    when = _PROPS["trigger_set_when"]["properties"]["when"]["properties"]
    assert when["conditions"]["properties"]["distance"]["type"] == "float"


def test_set_when_multiplayer_and_character_alternatives_documented():
    """#243 distance to entity 20000, #244 a character's state as an any_of
    alternative, #245 multiplayer_state."""
    note = _FIELD_NOTES["gate_set_when"]
    for word in (
        "multiplayer_state",
        "host / client / multiplayer / multiplayer_pending / singleplayer / "
        "invasion / invasion_pending",
        "20000",
        "or character states",
        "past 12 conditions",
    ):
        assert word in note, word
    assert _PROPS["gate_set_when"]["properties"]["state"]["type"] == "keyword"
