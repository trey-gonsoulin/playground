"""#308 consumable fields: sub_category, usage, crafting_container, thrown poise."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

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
    assert "potGroupId" in note and "max_held" in note
    assert "crafting_container" in _FIELD_NOTES["used_in"]


def test_thrown_poise_and_upgraded_type():
    proj = _PROPS["projectile"]["properties"]
    assert proj["poise_damage"] == {"type": "float"}
    assert "Hefty Furnace Pot 10" in _FIELD_NOTES["projectile"]
    assert "upgraded" in _FIELD_NOTES["acquisition_types"]
