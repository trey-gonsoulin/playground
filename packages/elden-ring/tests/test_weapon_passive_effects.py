"""Weapon passive effects (#163): weapons reuse the shared effect / effects mapping and
the notes document them. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]

# A decoded Blasphemous Blade doc's effect fields, as the builder emits them.
_BLASPHEMOUS_BLADE = {
    "effects": [
        {
            "stat": "HP restored",
            "value": 40.0,
            "unit": "points",
            "target": "self",
            "condition": "on defeating an enemy",
        },
        {
            "stat": "max HP restored",
            "value": 4.0,
            "unit": "%",
            "target": "self",
            "condition": "on defeating an enemy",
        },
    ],
    "effect": "40 HP restored (on defeating an enemy); "
    "4% max HP restored (on defeating an enemy)",
    "effect_value": 40.0,
}


def test_weapon_effect_fields_mapped():
    # The mapping is dynamic:strict, so every emitted leaf must already exist.
    for field in ("effect", "effect_value", "effect_duration"):
        assert field in _PROPS, field
    leaves = _PROPS["effects"]["properties"]
    for entry in _BLASPHEMOUS_BLADE["effects"]:
        for key in entry:
            assert key in leaves, key


def test_weapon_passives_documented():
    assert "weapons" in _FIELD_NOTES["effect"]
    assert "Blasphemous Blade" in _FIELD_NOTES["effect"]
    assert "full moon sorceries" in _FIELD_NOTES["effect"]
    assert "Golden Arrow" in _FIELD_NOTES["effects"]
    # Distinct from the in-game effect_text lines (#90).
    assert "effect_text" in _FIELD_NOTES["effect"]
    assert "#163" in _FIELD_NOTES["effect_text"]
