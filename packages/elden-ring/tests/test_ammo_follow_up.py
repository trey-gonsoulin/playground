"""Ammo follow-up hit motion values (#152): mapped under projectile and documented.
No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROJECTILE = INDEX_MAPPING["mappings"]["properties"]["projectile"]["properties"]


def test_follow_up_motion_values_mapped():
    mv = _PROJECTILE["follow_up_motion_values"]["properties"]
    for key in ("count", "physical", "magic", "fire", "lightning", "holy"):
        assert mv[key] == {"type": "integer"}, key


def test_follow_up_motion_values_documented():
    note = _FIELD_NOTES["projectile"]
    assert "follow_up_motion_values" in note
    assert "Lightning Greatbolt" in note and "30" in note


def test_weight_note_says_ammo_has_none():
    """Ammo docs drop EquipParamWeapon.weight: it doesn't count toward equip load (#286)."""
    note = _FIELD_NOTES["weight"]
    assert "Ammo has none" in note and "#286" in note
