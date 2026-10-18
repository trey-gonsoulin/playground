"""#308 consumable fields: sub_category, usage, crafting_container, thrown poise."""

from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def test_sub_category_and_usage_mapped():
    assert _PROPS["sub_category"] == {"type": "keyword"}
    usage = _PROPS["usage"]["properties"]
    for leaf in (
        "reusable",
        "horseback",
        "horseback_only",
        "ladder",
        "multiplayer",
        "offline",
        "bulk_use",
    ):
        assert usage[leaf] == {"type": "boolean"}, leaf
        assert leaf in _FIELD_NOTES["usage"], leaf
    for label in ("Pot", "Aromatic", "Grease", "Throwable", "Golden Rune", "Online"):
        assert label in _FIELD_NOTES["sub_category"], label


def test_crafting_container_mapped_and_documented():
    assert _PROPS["crafting_container"] == {"type": "keyword"}
    note = _FIELD_NOTES["crafting_container"]
    for tool in ("Cracked Pot", "Ritual Pot", "Hefty Cracked Pot", "Perfume Bottle"):
        assert tool in note, tool
    assert "max_held" in note
    assert "crafting_container" in _FIELD_NOTES["used_in"]


def test_thrown_poise_and_upgraded_type():
    proj = _PROPS["projectile"]["properties"]
    assert proj["poise_damage"] == {"type": "float"}
    assert "upgraded" in _FIELD_NOTES["acquisition_types"]


def test_system_lot_rule_documented():
    """#381/#384: map lots count only when handed out; #382 bulk_use names its items."""


def test_painting_tutorial_and_dialog_fields():
    """#316 paintings / info sub_category / item dialogs, #384 tutorial items."""
    for field in ("from_paintings", "painting_rewards", "from_tutorial"):
        assert _PROPS[field] == {"type": "keyword"}, field
        assert field in _FIELD_NOTES, field
    assert _PROPS["viewpoints"] == {"type": "object", "enabled": False}
    for field in ("grants_item", "raised_by_items"):
        assert _PROPS[field] == {"type": "keyword"}, field
        assert field in _FIELD_NOTES, field
    note = _FIELD_NOTES["acquisition_types"]
    assert "painting_reward" in note and "tutorial" in note
    assert "Simple Map" in _FIELD_NOTES["sub_category"]
