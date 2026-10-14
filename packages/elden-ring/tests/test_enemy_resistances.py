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
_NEGATION = {
    "physical",
    "strike",
    "slash",
    "pierce",
    "magic",
    "fire",
    "lightning",
    "holy",
}


def test_enemy_negation_mapped_and_documented():
    # Enemy damage negation % per type (#298): top level (shared with armor), per
    # variant stat block and per phase, with the armor keys.
    props = INDEX_MAPPING["mappings"]["properties"]
    for neg in (
        props["negation"],
        props["variants"]["properties"]["negation"],
        props["phases"]["properties"]["negation"],
    ):
        assert set(neg["properties"]) == _NEGATION
        assert all(v == {"type": "float"} for v in neg["properties"].values())
    assert "#298" in _FIELD_NOTES["negation"]
    assert "phases.negation" in _FIELD_NOTES and "variants.negation" in _FIELD_NOTES


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


def test_npc_param_values_mapped_and_documented():
    # Humanoids' own NpcParam poise / resistances, replaced by their armor (#285).
    props = INDEX_MAPPING["mappings"]["properties"]["npc_param_values"]["properties"]
    assert props["poise"] == {"type": "float"}
    resist = props["resistances"]["properties"]
    assert set(resist) == _RESISTS
    assert all(v == {"type": "integer"} for v in resist.values())
    assert "#285" in _FIELD_NOTES["npc_param_values"]
    assert "npc_param_values" in _FIELD_NOTES["resistances"]
    assert "npc_param_values" in _FIELD_NOTES["stats.poise"]


def test_combat_details_mapped_and_documented():
    # Poise regeneration, poise damage taken, per-proc resistance escalation (#302):
    # top level, per variant and per phase; summon_stats gets no new keys.
    props = INDEX_MAPPING["mappings"]["properties"]
    for where, prefix in (
        (props, ""),
        (props["variants"]["properties"], "variants."),
        (props["phases"]["properties"], "phases."),
    ):
        assert where["poise_regen_delay"] == {"type": "float"}
        assert where["poise_damage_taken_multiplier"] == {"type": "float"}
        inc = where["resistance_increase_per_proc"]["properties"]
        mult = where["resistance_multiplier_per_proc"]["properties"]
        assert set(inc) == set(mult) == _RESISTS
        assert all(v == {"type": "integer"} for v in inc.values())
        assert all(v == {"type": "float"} for v in mult.values())
        for f in (
            "poise_regen_delay",
            "poise_damage_taken_multiplier",
            "resistance_increase_per_proc",
            "resistance_multiplier_per_proc",
        ):
            assert prefix + f in _FIELD_NOTES, prefix + f
    summon = props["summon_stats"]["properties"]
    assert "poise_regen_delay" not in summon
    assert "resistance_increase_per_proc" not in summon
    assert "316 -> 414 / 704 / 1161" in _FIELD_NOTES["resistance_increase_per_proc"]
    assert "9.231" in _FIELD_NOTES["poise_regen_delay"]


def test_scripted_damage_taken_mapped_and_documented():
    # Scripted damage-taken states (#323): top level, per variant and per phase; not
    # on summon_stats, and not folded into negation.
    props = INDEX_MAPPING["mappings"]["properties"]
    for where, prefix in (
        (props, ""),
        (props["variants"]["properties"], "variants."),
        (props["phases"]["properties"], "phases."),
    ):
        sdt = where["scripted_damage_taken"]["properties"]
        assert sdt["speffect_id"] == {"type": "integer"}
        assert sdt["lifted_when"] == {"type": "keyword"}
        mult = sdt["multiplier"]["properties"]
        assert set(mult) == set(where["negation"]["properties"])
        assert all(v == {"type": "float"} for v in mult.values())
        # #380: the same row's status / poise multipliers, under the summon names.
        status = sdt["status_buildup_taken_multiplier"]["properties"]
        assert (
            status
            == props["summon_stats"]["properties"]["status_buildup_taken_multiplier"][
                "properties"
            ]
        )
        assert sdt["poise_damage_taken_multiplier"] == {"type": "float"}
        assert set(sdt["lifted_by"]["properties"]) == {
            "speffect_ids",
            "animation_ids",
            "event_ids",
        }
        assert prefix + "scripted_damage_taken" in _FIELD_NOTES
    assert "scripted_damage_taken" not in props["summon_stats"]["properties"]
    for leaf in (
        "speffect_id",
        "multiplier",
        "status_buildup_taken_multiplier",
        "poise_damage_taken_multiplier",
        "lifted_when",
        "lifted_by",
    ):
        assert "scripted_damage_taken." + leaf in _FIELD_NOTES
    assert "scripted_damage_taken" in _FIELD_NOTES["negation"]
    assert "90%" in _FIELD_NOTES["scripted_damage_taken"]


def test_parryable_mapped_and_documented():
    # Enemy attack animations with a TAE get-parried window (#302); enemy attacks only.
    props = INDEX_MAPPING["mappings"]["properties"]
    attacks = props["attacks"]["properties"]
    assert attacks["parryable"] == {"type": "boolean"}
    assert attacks["parryable_animations"] == {"type": "integer"}
    assert attacks["attack_animations"] == {"type": "integer"}
    summon_attacks = props["summon_stats"]["properties"]["attacks"]["properties"]
    assert "parryable" not in summon_attacks
    note = _FIELD_NOTES["attacks.parryable"]
    assert "Commander Gaius false" in note and "Grafted Scion true" in note
    assert "attacks.parryable_animations" in _FIELD_NOTES
    assert "attacks.attack_animations" in _FIELD_NOTES
