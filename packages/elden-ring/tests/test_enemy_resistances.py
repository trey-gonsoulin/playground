"""Mapping checks for enemy resistances + traits (#84). No OpenSearch needed."""

from elden_ring._client import _FIELD_NOTES, INDEX_MAPPING

_RESISTS = {
    "poison",
    "scarlet_rot",
    "bleed",
    "frostbite",
    "sleep",
    "madness",
    "death_blight",
}


def test_resistance_and_trait_fields_mapped():
    # Declared before the reindex so the dynamic text+keyword default never locks in.
    props = INDEX_MAPPING["mappings"]["properties"]
    resist = props["resistances"]["properties"]
    assert set(resist) == _RESISTS
    assert all(v == {"type": "integer"} for v in resist.values())
    assert props["immune_to"] == {"type": "keyword"}
    assert props["traits"] == {"type": "keyword"}
    assert props["weak_point_damage_multiplier"] == {"type": "float"}


def test_resistance_and_trait_fields_documented():
    assert "999 = immune" in _FIELD_NOTES["resistances"]
    for trait in (
        "weak_to_gravity",
        "lives_in_death",
        "ancient_dragon",
        "dragon",
        "undead",
    ):
        assert trait in _FIELD_NOTES["traits"]
    assert "immune_to" in _FIELD_NOTES
    assert "weak_point_damage_multiplier" in _FIELD_NOTES
    assert "humanoid subset" not in _FIELD_NOTES["stats.hp"]
