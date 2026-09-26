"""Schema v2 checks (#115): grouped stat objects, keyword cleanup, strict mapping.
No OpenSearch needed."""

from elden_ring._client import _flatten, INDEX_MAPPING

_PROPS = INDEX_MAPPING["mappings"]["properties"]


def _mapped(path: str) -> dict | None:
    props, spec = _PROPS, None
    for part in path.split("."):
        spec = props.get(part)
        if spec is None:
            return None
        if spec.get("enabled") is False:  # stored, not indexed: covers its subtree
            return spec
        props = spec.get("properties", {})
    return spec


def test_mapping_is_strict():
    assert INDEX_MAPPING["mappings"]["dynamic"] == "strict"


def test_closed_vocabularies_are_plain_keyword():
    for f in ("affinity", "availability", "drops", "spell_role"):
        assert _PROPS[f] == {"type": "keyword"}, f


def test_flat_stat_fields_removed():
    for f in (
        "attack_physical",
        "scaling_str",
        "req_dex",
        "defense_fire",
        "negation_slash",
        "hp",
        "magic_defense",
        "achievement_set",
    ):
        assert f not in _PROPS, f


def test_grouped_leaves_mapped():
    expected = {
        "attack_power.holy": "integer",
        "scaling.arc.grade": "keyword",
        "scaling.arc.value": "float",
        "requirements.fai": "integer",
        "negation.pierce": "float",
        "negation.lightning": "float",
        "stats.hp": "integer",
        "stats.poise": "float",
        "defense.holy": "float",
        "resistances.bleed": "integer",
        "attack_power.stamina": "integer",
        "attack_power.critical": "integer",
        "guard.physical": "float",
        "guard.holy": "float",
        "guard.boost": "float",
        "guard.resistances.bleed": "float",
        "guard.resistances.death_blight": "float",
        "damage_types": "keyword",
        "status_buildup.bleed": "integer",
        "status_buildup.death_blight": "integer",
        "reinforce_type_id": "integer",
        "max_level.level": "integer",
        "max_level.attack_power.physical": "integer",
        "max_level.scaling.str.grade": "keyword",
        "max_level.guard.boost": "float",
        "max_level.status_buildup.frostbite": "integer",
        "summon_count": "integer",
        "summon_stats.count": "integer",
        "summon_stats.stats.hp": "integer",
        "summon_stats.resistances.sleep": "integer",
        "summon_stats.immune_to": "keyword",
        "summon_stats.damage_multiplier": "float",
        "max_level.summon_count": "integer",
        "max_level.summon_stats.count": "integer",
        "max_level.summon_stats.stats.hp": "integer",
        "max_level.summon_stats.resistances.sleep": "integer",
        "max_level.summon_stats.damage_multiplier": "float",
        "poise_damage.one_handed.r1": "float",
        "poise_damage.two_handed.guard_counter": "float",
        "poise_damage.pvp.two_handed.charged_r2": "float",
        "poise_damage.one_handed.powerstance": "float",
        "poise_damage.two_handed.jumping_r2": "float",
        "poise_damage.pvp.one_handed.running_r1": "float",
        "poise_damage.one_handed.crouch_r1": "float",
        "poise_damage.two_handed.rolling_r1": "float",
        "poise_damage.one_handed.left_r1": "float",
        "poise_damage.one_handed.mounted_charged_r2": "float",
        "poise_damage.one_handed.mounted_left_jumping_r2": "float",
        "poise_damage.pvp.one_handed.powerstance_backstep": "float",
        "attacks.behavior_variation": "integer",
        "attacks.count": "integer",
        "attacks.damage_types": "keyword",
        "attacks.elements": "keyword",
        "attacks.attack_power.holy": "integer",
        "attacks.status_buildup.scarlet_rot": "integer",
        "attacks.status_effects": "keyword",
        "attacks.shared_with": "keyword",
        "equipment.weapons": "keyword",
        "equipment.ashes_of_war": "keyword",
        "equipment.spells": "keyword",
        "equipment.ammo": "keyword",
        "equipped_by": "keyword",
    }
    for path, type_ in expected.items():
        assert (_mapped(path) or {}).get("type") == type_, path
    assert _mapped("defense.physical") is None  # NpcParam has no physical defense


def test_upgrade_curve_not_indexed():
    # Per-level arrays are returned, never searched (#112).
    assert _PROPS["upgrade_curve"] == {"type": "object", "enabled": False}
    assert _PROPS["poise_damage_chains"] == {"type": "object", "enabled": False}  # #119


def test_builder_doc_shapes_fully_mapped():
    # One doc per grouped shape the builders emit; strict mapping rejects any stray leaf.
    docs = [
        {
            "attack_power": {"physical": 96, "stamina": 61, "critical": 130},
            "scaling": {"str": {"grade": "B", "value": 97.2}},
            "guard": {
                "physical": 52.25,
                "fire": 40.25,
                "boost": 42.0,
                "resistances": {"scarlet_rot": 14.25, "frostbite": 14.25},
            },
            "requirements": {"str": 14, "dex": 12},
            "depicted_in_talisman": "Dagger Talisman",
            "damage_types": ["Standard", "Pierce"],
            "status_buildup": {"bleed": 38, "frostbite": 66},
            "reinforce_type_id": 0,
            "poise_damage": {
                "one_handed": {
                    "r1": 3.0,
                    "r2": 6.0,
                    "charged_r2": 18.0,
                    "jumping_r2": 12.0,
                    "powerstance": 1.8,
                },
                "two_handed": {"r1": 3.9, "guard_counter": 4.5},
                "pvp": {
                    "one_handed": {"r1": 40.5},
                    "two_handed": {"charged_r2": 594.0},
                },
            },
            "poise_damage_chains": {"pvp": {"one_handed": {"r1": [40.5, 63.0]}}},
            "max_level": {
                "level": 25,
                "attack_power": {"physical": 306, "stamina": 122, "critical": 100},
                "scaling": {"str": {"grade": "C", "value": 81.0}},
                "guard": {"boost": 50.4, "resistances": {"bleed": 15.0}},
                "status_buildup": {"bleed": 38, "frostbite": 105},
            },
        },
        {"negation": {"physical": 10.0, "strike": 12.0, "holy": 4.0}},
        {"requirements": {"int": 18}, "fp_cost": 12, "spell_role": "Offensive"},
        {
            "stats": {"hp": 3186, "stamina": 150, "poise": 80.0},
            "defense": {"magic": 100, "fire": 100, "lightning": 100, "holy": 100},
            "resistances": {"poison": 154},
            "attacks": {
                "behavior_variation": 30500,
                "count": 61,
                "damage_types": ["Slash", "Strike"],
                "elements": ["physical", "lightning"],
                "attack_power": {"physical": 300, "lightning": 250},
                "status_buildup": {"scarlet_rot": 130, "frostbite": 130},
                "status_effects": ["scarlet_rot", "frostbite"],
                "shared_with": ["Commander O'Neil"],
            },
            "equipment": {
                "weapons": ["Great Stars", "Clawmark Seal"],
                "ashes_of_war": ["Ash of War: Lion's Claw"],
                "armor": ["Page Hood"],
                "spells": ["Beast Claw"],
                "talismans": ["Sacred Scorpion Charm"],
                "ammo": ["Arrow"],
            },
        },
        {"equipped_by": ["Recusant Henricus"]},
        {
            "summon_count": 3,
            "summon_stats": [
                {
                    "count": 3,
                    "stats": {"hp": 500, "stamina": 50, "poise": 35.0},
                    "defense": {"magic": 100, "holy": 100},
                    "resistances": {"sleep": 84, "bleed": 999},
                    "immune_to": ["bleed"],
                    "traits": [],
                    "weak_point_damage_multiplier": 1.5,
                    "damage_multiplier": 1.0,
                }
            ],
            "max_level": {
                "level": 10,
                "summon_count": 3,
                "summon_stats": [
                    {
                        "count": 3,
                        "stats": {"hp": 3711, "stamina": 94, "poise": 35.0},
                        "defense": {"magic": 120.0},
                        "resistances": {"sleep": 200, "bleed": 999},
                        "immune_to": ["bleed"],
                        "traits": [],
                        "damage_multiplier": 3.796,
                    }
                ],
            },
            "upgrade_curve": {"summon_stats": [{"stats": {"hp": [500, 3711]}}]},
        },
    ]
    for doc in docs:
        for path, v in _flatten(doc).items():
            assert _mapped(path) is not None, path
            for item in v if isinstance(v, list) else []:
                if isinstance(item, dict):  # object arrays (summon_stats)
                    for sub in _flatten(item, f"{path}."):
                        assert _mapped(sub) is not None, sub


def test_flatten_to_dotted_leaves():
    doc = {"name": "Halberd", "attack_power": {"physical": 134}, "tags": ["Halberd"]}
    assert _flatten(doc) == {
        "name": "Halberd",
        "attack_power.physical": 134,
        "tags": ["Halberd"],
    }
    assert _flatten({"scaling": {"str": {"grade": "D"}}}) == {"scaling.str.grade": "D"}
