"""Quest entity (#95): every leaf the builder emits is mapped and documented.
No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# Shape of a built 1.17 quest doc (Boc / Millicent), trimmed.
_DOC = {
    "entity_type": "quest",
    "name": "Demi-Human Boc",
    "name_ja": "亜人のボック",
    "patch_version": "1.17.0",
    "source": "EMEVD",
    "npc": "Demi-Human Boc",
    "npc_names": ["Demi-Human Boc", "Boc the Seamster"],
    "flag_block": [3940, 3959],
    "locations": ["Limgrave", "Coastal Cave"],
    "related_npcs": ["Smithing Master Hewg"],
    "text_content": "Demi-Human Boc\nBoc the Seamster\nLimgrave",
    "steps": [
        {"phase_flag": 3945, "order": 1, "locations": ["Limgrave"]},
        {
            "phase_flag": 3948,
            "entered_from": 3947,
            "order": 4,
            "locations": ["Academy of Raya Lucaria"],
            "when": [
                {
                    "kind": "flag",
                    "flag": 1039409260,
                    "negated": True,
                    "set_at": ["Liurnia of the Lakes"],
                },
                {
                    "kind": "boss_defeated",
                    "flag": 9118,
                    "bosses": ["Rennala, Queen of the Full Moon"],
                },
                {"kind": "talk", "flag": 31159206, "npcs": ["Demi-Human Boc"]},
                {"kind": "invasion", "flag": 7610},
                {"kind": "item_pickup", "flag": 1, "items": ["Larval Tear"]},
                {"kind": "item_held", "items": ["Larval Tear"]},
                {"kind": "item_held", "item_id": 8185},
                {
                    "kind": "quest_phase",
                    "flag": 4895,
                    "npc": "Needle Knight Leda",
                    "quest": "Needle Knight Leda (4880–4899)",
                },
                {
                    "kind": "any_of",
                    "conditions": [
                        {"kind": "talk", "flag": 1043379353, "npcs": ["Rya"]},
                        {"kind": "flag", "flag": 1043379354, "negated": True},
                    ],
                },
            ],
        },
    ],
    "outcomes": [
        {"flag": 3941, "slot": 1, "trigger": "attacked", "life_state": "hostile"},
        # #208: set by a map event whose only conditions weren't listable; #213:
        # what it waited for
        {
            "flag": 3942,
            "slot": 2,
            "trigger": "event",
            "life_state": "hostile",
            "waited_for": [
                {
                    "kind": "character",
                    "state": "dead",
                    "npc": "Moore",
                    "entity_ids": [2047460720, 2047460721],
                },
                {"kind": "flag", "flag": 1034509403, "set_at": ["Liurnia"]},
                # #218: the map flag the NPC's hit counter sets
                {
                    "kind": "hit_count",
                    "flag": 1035429251,
                    "npc": "Ranni the Witch",
                    "entity_ids": [1035420706],
                    "hits": 3,
                },
            ],
        },
        {
            "flag": 3943,
            "slot": 3,
            "life_state": "dead",
            "when": [
                {
                    "kind": "life_state",
                    "flag": 3941,
                    "npc": "Demi-Human Boc",
                    "life_state": "hostile",
                }
            ],
        },
    ],
}


def _leaves(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v, path)
    else:
        yield path


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        props = spec.get("properties", {})
    return spec


def test_quest_doc_fully_mapped():
    paths = set(_leaves(_DOC))
    assert "steps.when.bosses" in paths and "outcomes.when.life_state" in paths
    for path in paths:
        spec = _mapped(path)
        assert spec is not None and "type" in spec, path


def test_quest_flags_are_long():
    # 10-digit tile flags (2045429298) sit near the int32 limit: long, like defeat_flag.
    for path in (
        "flag_block",
        "steps.phase_flag",
        "steps.entered_from",
        "steps.when.flag",
        "outcomes.flag",
        "outcomes.waited_for.flag",
        "outcomes.waited_for.entity_ids",
        "steps.when.entity_ids",
        "steps.when.conditions.entity_ids",
    ):
        assert _mapped(path)["type"] == "long", path


def test_quest_fields_documented():
    for f in (
        "npc",
        "npc_names",
        "flag_block",
        "related_npcs",
        "steps",
        "steps.when",
        "outcomes",
        "outcomes.trigger",
        "outcomes.waited_for",
    ):
        assert f in _FIELD_NOTES, f
    assert "waited_for" in _FIELD_NOTES["outcomes.trigger"]
    assert "quest doc" in _FIELD_NOTES["locations"]
    for kind in (
        "boss_defeated",
        "invasion",
        "item_pickup",
        "item_held",
        "talk",
        "quest_phase",
        "life_state",
        "set_at",
        "any_of",
        "conditions",
        "hit_count",
        "hits",
    ):
        assert kind in _FIELD_NOTES["steps.when"], kind
