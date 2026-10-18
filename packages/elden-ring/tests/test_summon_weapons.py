"""#418: summon_stats.weapons labels spirits told apart only by display mask."""

from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_summon_weapons_mapped_at_every_level():
    for stats in (
        _PROPS["summon_stats"]["properties"],
        _PROPS["max_level"]["properties"]["summon_stats"]["properties"],
    ):
        assert stats["weapons"] == {"type": "keyword"}


def test_summon_weapons_note():
    note = _FIELD_NOTES["summon_stats.weapons"]
    assert "model_display_masks" in note
    assert "summon_stats.weapons" in _FIELD_NOTES["summon_stats.model_display_masks"]
    assert "summon_stats.weapons" in _FIELD_NOTES["summon_stats.npc_param_id"]
