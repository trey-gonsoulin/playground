"""#418: summon_stats.weapons labels spirits told apart only by display mask."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_summon_weapons_mapped_at_every_level():
    for stats in (
        _PROPS["summon_stats"]["properties"],
        _PROPS["max_level"]["properties"]["summon_stats"]["properties"],
    ):
        assert stats["weapons"] == {"type": "keyword"}


def test_summon_weapons_note():
    note = _FIELD_NOTES["summon_stats.weapons"]
    assert "Dagger / Short Sword" in note and "model_display_masks" in note
    assert "Torch" in note and "Fextralife" in note
    assert "Heavy Crossbow" in note and "that have a drop lot" in note
    assert "summon_stats.weapons" in _FIELD_NOTES["summon_stats.model_display_masks"]
    assert "summon_stats.weapons" in _FIELD_NOTES["summon_stats.npc_param_id"]
