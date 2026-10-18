"""Weapon / Ash of War skill objects (#289) and Ash of War affinities and weapon
classes (#290): mapped and documented. No OpenSearch needed."""

from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_skill_mapped():
    skill = _PROPS["skill"]["properties"]
    assert skill["name"] == {"type": "keyword"}
    assert skill["caption"] == {"type": "text"}
    assert skill["fp_cost"] == {"type": "integer"}
    assert skill["chargeable"] == {"type": "boolean"}
    follow = skill["follow_up_fp_cost"]["properties"]
    assert follow == {"light": {"type": "integer"}, "heavy": {"type": "integer"}}
    assert _PROPS["default_ash_of_war"] == {"type": "keyword"}  # unchanged


def test_ash_affinity_fields_mapped():
    for key in ("default_affinity", "affinities", "weapon_classes"):
        assert _PROPS[key] == {"type": "keyword"}, key


def test_documented():
    note = _FIELD_NOTES["skill"]
    for key in ("fp_cost", "follow_up_fp_cost.light", "chargeable", "caption"):
        assert key in note, key
    assert "Sacred" in _FIELD_NOTES["default_affinity"]
    assert "Flame Art" in _FIELD_NOTES["affinities"]
    assert "Thrusting Shield" in _FIELD_NOTES["weapon_classes"]


def test_skill_attacks_and_effects_mapped():  # #326
    skill = _PROPS["skill"]["properties"]
    hit = skill["attacks"]["properties"]
    for key in ("motion_values", "attack_power"):
        assert set(hit[key]["properties"]) == {
            "physical",
            "magic",
            "fire",
            "lightning",
            "holy",
        }
    assert hit["stamina_cost"] == {"type": "integer"}
    assert hit["active_frames"]["properties"]["start"] == {"type": "integer"}
    assert hit["hit_label"] == {"type": "keyword"}
    assert hit["projectile"] == {"type": "boolean"}
    assert hit["hit_count"] == {"type": "integer"}
    # skill buffs share the item effects mapping
    assert skill["effects"] == _PROPS["effects"]
    assert skill["effect"] == {"type": "text"}
    assert skill["effect_duration"] == {"type": "float"}
    for key in (
        "attacks",
        "attacks.motion_values",
        "attacks.attack_power",
        "attacks.stamina_cost",
        "attacks.active_frames",
        "effects",
    ):
        assert f"skill.{key}" in _FIELD_NOTES, key
    # #431: recovery per follow-up action and the animation's length
    assert set(hit["recovery_frames"]["properties"]) == {
        "dodge",
        "right_hand_attack",
        "left_hand_attack",
        "skill",
        "guard",
        "quick_item",
        "item",
        "move",
    }
    assert hit["recovery_frames"]["properties"]["dodge"] == {"type": "integer"}
    assert hit["total_frames"] == {"type": "integer"}
