"""Weapon / Ash of War skill objects (#289) and Ash of War affinities and weapon
classes (#290): mapped and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_skill_mapped():
    skill = _PROPS["skill"]["properties"]
    assert skill["name"] == {"type": "keyword"}
    assert skill["caption"] == {"type": "text"}
    assert skill["fp_cost"] == {"type": "integer"}
    assert skill["chargeable"] == {"type": "boolean"}
    follow = skill["follow_up_fp_cost"]["properties"]
    assert follow == {"r1": {"type": "integer"}, "r2": {"type": "integer"}}
    assert _PROPS["default_ash_of_war"] == {"type": "keyword"}  # unchanged


def test_ash_affinity_fields_mapped():
    for key in ("default_affinity", "affinities", "weapon_classes"):
        assert _PROPS[key] == {"type": "keyword"}, key


def test_documented():
    note = _FIELD_NOTES["skill"]
    for key in ("fp_cost", "follow_up_fp_cost.r1", "chargeable", "caption"):
        assert key in note, key
    assert "26 (-/12)" in note and "Wall of Sparks" in note
    assert "Sacred" in _FIELD_NOTES["default_affinity"]
    assert "Flame Art" in _FIELD_NOTES["affinities"]
    assert "whetblade" in _FIELD_NOTES["affinities"].lower()
    assert "Thrusting Shield" in _FIELD_NOTES["weapon_classes"]
