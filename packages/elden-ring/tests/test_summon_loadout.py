"""#310: legendary spirit ashes + the humanoid spirits' loadout gear on summon_stats."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_summon_loadout_groups_mapped_like_enemy():
    for stats in (
        _PROPS["summon_stats"]["properties"],
        _PROPS["max_level"]["properties"]["summon_stats"]["properties"],
    ):
        for group in ("equipment", "weapon_attacks", "spell_attacks"):
            assert stats[group] == _PROPS[group], group
    wa = _PROPS["summon_stats"]["properties"]["weapon_attacks"]["properties"]
    assert wa["weapon"] == {"type": "keyword"}
    assert wa["attack_power"]["properties"]["physical"] == {"type": "integer"}


def test_summon_loadout_and_legendary_notes():
    note = _FIELD_NOTES["summon_stats.equipment"]
    assert "NpcPlayerInitParamId" in note and "Sword of Milos" in note
    assert "Mimic Tear" in note
    assert "weapon_attacks" in _FIELD_NOTES["summon_stats"]
    leg = _FIELD_NOTES["is_legendary"]
    assert "Lhutel the Headless" in leg and "spirit_ash" in leg
    assert _PROPS["is_legendary"] == {"type": "boolean"}
