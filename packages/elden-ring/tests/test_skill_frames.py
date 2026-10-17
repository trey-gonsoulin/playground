"""Skill-level frames (#477) and Ash of War per-class hit lists (#432): mapped and
documented. No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_SKILL = INDEX_MAPPING["mappings"]["properties"]["skill"]["properties"]


def test_frames_mapped():
    frames = _SKILL["frames"]["properties"]
    assert frames["startup"] == {"type": "integer"}
    assert frames["total"] == {"type": "integer"}
    for window in ("invulnerable", "parry"):
        assert set(frames[window]["properties"]) == {"start", "end"}
    # the same follow-up actions as skill.attacks.recovery_frames (#431)
    assert (
        frames["recovery"]["properties"]
        == _SKILL["attacks"]["properties"]["recovery_frames"]["properties"]
    )
    assert "skill.frames" in _FIELD_NOTES


def test_attacks_by_class_mapped():
    by_class = _SKILL["attacks_by_class"]["properties"]
    assert by_class["weapon_classes"] == {"type": "keyword"}
    # each class list has the shared list's fields
    assert by_class["attacks"] == _SKILL["attacks"]
    assert "skill.attacks_by_class" in _FIELD_NOTES
    assert "skill.attacks_by_class.weapon_classes" in _FIELD_NOTES
