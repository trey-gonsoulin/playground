"""Ammo bow-skill shots (#151): mapped and documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_SHOTS = INDEX_MAPPING["mappings"]["properties"]["skill_shots"]["properties"]


def test_skill_shots_mapped():
    assert _SHOTS["ammo"] == {"type": "keyword"}  # #177
    assert _SHOTS["skill"] == {"type": "keyword"}
    assert _SHOTS["hit_label"] == {"type": "keyword"}
    assert _SHOTS["hit_count"] == {"type": "integer"}
    assert _SHOTS["hit_count_max"] == {"type": "integer"}  # #176
    assert _SHOTS["poise_damage"] == {"type": "float"}
    for key in ("physical", "magic", "fire", "lightning", "holy"):
        assert _SHOTS["motion_values"]["properties"][key] == {"type": "integer"}, key
    flight = _SHOTS["projectile"]["properties"]
    for key in ("speed", "max_speed", "range", "gravity", "lifetime", "hit_radius"):
        assert flight[key] == {"type": "float"}, key


def test_skill_shots_documented():
    note = _FIELD_NOTES["skill_shots"]
    assert "Mighty Shot" in note and "hit_count" in note and "motion_values" in note
    assert "skill_shots" in _FIELD_NOTES["projectile"]


def test_rain_hit_count_and_bow_summary_documented():
    """#176: the Rain skills' modelled re-hits; #177: the bow / Ash of War entries."""
    note = _FIELD_NOTES["skill_shots"]
    assert "Rain of Arrows 6" in note and "Radahn's Rain 8" in note
    assert "lower bound" not in note
    assert "hit_count_max" in note and "Rain of Arrows 7" in note
    assert "Kick" not in note  # skill-less crossbows / ballistae (SwordArts 4990)
    assert "Ash of War: Barrage" in note and "skill_shots.ammo" in note
    assert "Great Arrow" in _FIELD_NOTES["skill_shots.ammo"]
