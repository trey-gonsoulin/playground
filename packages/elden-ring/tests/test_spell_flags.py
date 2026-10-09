"""#293 spell stamina / hold / cast flags and #294 spell schools: mapping + notes."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_spell_cost_fields_mapped_and_documented():
    for leaf in ("stamina_cost", "stamina_cost_charged", "fp_cost_continuous"):
        assert _PROPS[leaf] == {"type": "integer"}, leaf
        assert leaf in _FIELD_NOTES, leaf
    assert _PROPS["cast_hold"] == {"type": "keyword"}
    for leaf in ("is_horseback_castable", "is_weapon_buff", "is_shield_buff"):
        assert _PROPS[leaf] == {"type": "boolean"}, leaf
        assert leaf in _FIELD_NOTES, leaf
    assert "Rain of Fire 40" in _FIELD_NOTES["stamina_cost_charged"]
    assert "'30 (10)'" in _FIELD_NOTES["fp_cost_continuous"]
    assert "'continuous'" in _FIELD_NOTES["cast_hold"]


def test_spell_attacks_per_cast_buildup_and_guard_pierce():
    sa = _PROPS["spell_attacks"]["properties"]
    assert sa["guard_pierce"] == {"type": "integer"}
    for cast in ("uncharged", "charged"):
        props = sa[cast]["properties"]
        assert props["status_buildup"] == {"type": "object", "enabled": False}
        assert props["guard_pierce"] == {"type": "integer"}
    assert "Frenzied Burst madness 90 / 105" in _FIELD_NOTES["spell_attacks"]


def test_spell_schools_mapped_and_documented():
    assert _PROPS["spell_schools"] == {"type": "keyword"}
    assert _PROPS["school_boosted_by"] == {"type": "keyword"}
    assert "gravity sorceries" in _FIELD_NOTES["spell_schools"]
    assert "Snow Witch Hat" in _FIELD_NOTES["school_boosted_by"]
