"""Schema v2 checks (#115): grouped stat objects, keyword cleanup, strict mapping.
No OpenSearch needed."""

from elden_ring import mcp_server
from elden_ring._client import _FIELD_NOTES, _flatten, INDEX_MAPPING

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
        "summon_stats.damage_vs_enemies_multiplier": "float",
        "max_level.summon_stats.damage_vs_enemies_multiplier": "float",
        "summon_stats.attacks.behavior_variation": "integer",
        "summon_stats.attacks.attack_power.holy": "integer",
        "summon_stats.attacks.status_buildup.bleed": "integer",
        "summon_stats.attacks.status_effects": "keyword",
        "max_level.summon_stats.attacks.attack_power.physical": "integer",
        "summon_stats.damage_taken_multiplier.pierce": "float",
        "max_level.summon_stats.damage_taken_multiplier.holy": "float",
        "summon_stats.poise_damage_taken_multiplier": "float",
        "max_level.summon_stats.poise_damage_taken_multiplier": "float",
        "summon_stats.status_buildup_taken_multiplier.death_blight": "float",
        "max_level.summon_stats.status_buildup_taken_multiplier.poison": "float",
        "attacks.state_variants.special_states": "integer",
        "attacks.state_variants.placements": "integer",
        "attacks.state_variants.npc_param_ids": "integer",
        "attacks.state_variants.status_buildup.sleep": "integer",
        "attacks.state_variants.elements": "keyword",
        "attacks.other_tables.behavior_variation": "integer",
        "attacks.other_tables.placements": "integer",
        "attacks.other_tables.npc_param_ids": "integer",
        "attacks.other_tables.shared_with": "keyword",
        "attacks.other_tables.attack_power.physical": "integer",
        "attacks.other_tables.status_effects": "keyword",
        "attacks.all_status_effects": "keyword",
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
        "grabs.count": "integer",
        "grabs.damage_types": "keyword",
        "grabs.elements": "keyword",
        "grabs.attack_power.fire": "integer",
        "grabs.status_buildup.bleed": "integer",
        "grabs.status_effects": "keyword",
        # #253: max per-hit poise damage of enemy / summon attack profiles.
        "attacks.poise_damage": "float",
        "attacks.state_variants.poise_damage": "float",
        "attacks.other_tables.poise_damage": "float",
        "grabs.poise_damage": "float",
        "summon_stats.attacks.poise_damage": "float",
        "max_level.summon_stats.attacks.poise_damage": "float",
        "critical_hits.backstab": "boolean",
        "critical_hits.riposte": "boolean",
        "critical_hits.stance_break": "boolean",
        "critical_hits.downed": "boolean",
        "critical_hits.sleep": "boolean",
        "critical_hits.other_throw_types": "integer",
        "phases.critical_hits.stance_break": "boolean",
        "phases.critical_hits.sleep": "boolean",
        "phases.critical_hits.other_throw_types": "integer",
        "team_type": "integer",
        "team": "keyword",
        "ai.think_id": "integer",
        "ai.sight_distance": "integer",
        "ai.smell_distance": "integer",
        "ai.leash_distance": "integer",
        "ai.team_attack_weight": "integer",
        "ai.guards": "boolean",
        "stats_scaled.hp.min": "integer",
        "stats_scaled.hp.max": "integer",
        "stats_scaled.hp.placements": "integer",
        "stats_scaled.stamina.min": "integer",
        "stats_scaled.defense.holy.max": "integer",
        "stats_scaled.resistances.bleed.min": "integer",
        "stats_scaled.ng_plus.hp.max": "integer",
        "variants.stats_scaled.resistances.poison.max": "integer",
        "variants.stats_scaled.ng_plus.hp.min": "integer",
        "phases.stats_scaled.stamina": "integer",
        "phases.stats_scaled.defense.fire": "integer",
        "phases.stats_scaled.resistances.sleep": "integer",
        "phases.stats_scaled.ng_plus.hp": "integer",
        "stats_scaled.ng_plus.stamina.min": "integer",
        "stats_scaled.ng_plus.defense.lightning.max": "integer",
        "stats_scaled.ng_plus.resistances.death_blight.min": "integer",
        "variants.stats_scaled.ng_plus.defense.magic.min": "integer",
        "variants.stats_scaled.ng_plus.resistances.frostbite.max": "integer",
        "phases.stats_scaled.ng_plus.stamina": "integer",
        "phases.stats_scaled.ng_plus.defense.holy": "integer",
        "phases.stats_scaled.ng_plus.resistances.bleed": "integer",
        "variants.npc_ids": "keyword",
        "variants.npc_param_ids": "integer",
        "variants.stats.hp": "integer",
        "variants.resistances.scarlet_rot": "integer",
        "variants.immune_to": "keyword",
        "variants.stats_scaled.hp.max": "integer",
        "variants.maps": "keyword",
        "variants.name": "keyword",
        "variants.affinity": "keyword",
        "variants.rank": "integer",
        "variants.differs": "keyword",
        "variants.attack_power.physical": "integer",
        "variants.scaling.str.grade": "keyword",
        "variants.status_buildup.bleed": "integer",
        "variants.weight": "float",
        "variants.effect_value": "float",
        "effects.stat": "keyword",
        "effects.value": "float",
        "effects.pvp_value": "float",
        "effects.condition": "keyword",
        "effects.duration": "float",
        "effect_duration": "float",
        "variants.is_legendary": "boolean",
        "variants.text_differs": "boolean",
        "variants.availability": "keyword",
        "phases.phase": "integer",
        "phases.name": "keyword",
        "phases.npc_id": "keyword",
        "phases.npc_param_id": "integer",
        "phases.stats.hp": "integer",
        "phases.immune_to": "keyword",
        "phases.stats_scaled.hp": "integer",
        "phases.ends_at_hp_ratio": "float",
        "phases.hp_pool_shared_with": "keyword",
        "phases.heals_on_entry": "boolean",
        "enemies": "keyword",
        "region": "keyword",
        "nearest_grace": "keyword",
        "map": "keyword",
        "arena_position.x": "float",
        "arena_position.z": "float",
        "runes": "integer",
        "banner": "keyword",
        "defeat_flag": "long",
        "boss_encounters": "keyword",
        "npc_summons.npc": "keyword",
        "npc_summons.npc_id": "integer",
        "npc_summons.sign": "keyword",
        "npc_summons.requires_flag": "long",
        "summonable_for": "keyword",
        "hostile_signs.kind": "keyword",
        "hostile_signs.map": "keyword",
        "hostile_signs.sign_type": "integer",
        "hostile_signs.requires_flag": "long",
        "parent_region": "keyword",
        "position.x": "float",
        "world_position.z": "float",
        "entity_id": "long",
        "unlock_flag": "long",
        "bosses": "keyword",
        "kind": "keyword",
        "graces": "keyword",
        "area_scaling.speffect_id": "long",
        "area_scaling.placements": "integer",
        "area_scaling.hp": "float",
        "area_scaling.resistance": "float",
        "maps": "keyword",
        "regions": "keyword",
        "areas": "keyword",
        "locations": "keyword",
        "drop_regions": "keyword",
        "drop_locations": "keyword",
        "equipment.weapons": "keyword",
        "equipment.ashes_of_war": "keyword",
        "equipment.spells": "keyword",
        "equipment.ammo": "keyword",
        "weapon_attacks.weapon": "keyword",
        "weapon_attacks.reinforce_level": "integer",
        "weapon_attacks.attack_power.physical": "integer",
        "weapon_attacks.status_buildup.bleed": "integer",
        "weapon_attacks.poise_damage": "float",
        "spell_attacks.spell": "keyword",
        "spell_attacks.magic_id": "integer",  # #268
        "spell_attacks.elements": "keyword",
        "spell_attacks.status_effects": "keyword",
        "spell_attacks.attack_power.magic": "integer",
        "spell_attacks.poise_damage": "float",
        "equipped_by": "keyword",
        "given_by": "keyword",
        "in_exchange_for": "keyword",
        "in_exchange_count": "integer",
        "exchanged_for": "keyword",
        "duplication.service": "keyword",
        "duplication.where": "keyword",
        "duplication.price": "integer",
        "duplication.quantity": "integer",
        "duplication.currency": "keyword",
        "duplication.unlock_flag": "long",
        "duplication.unlocked_by_defeating": "keyword",
        "starting_classes": "keyword",
    }
    for path, type_ in expected.items():
        assert (_mapped(path) or {}).get("type") == type_, path
    assert _mapped("defense.physical") is None  # NpcParam has no physical defense


def test_upgrade_curve_not_indexed():
    # Per-level arrays are returned, never searched (#112).
    assert _PROPS["upgrade_curve"] == {"type": "object", "enabled": False}
    assert _PROPS["shop_listings"] == {"type": "object", "enabled": False}  # #89
    assert _PROPS["unlocks_shop_items"] == {"type": "keyword"}  # #144
    assert _PROPS["poise_damage_chains"] == {"type": "object", "enabled": False}  # #119
    assert _mapped("skill_poise_damage.max")["type"] == "float"  # #125
    assert _mapped("skill_poise_damage.pvp.hits")["type"] == "float"
    assert _mapped("skill_poise_damage.projectile_hits")["type"] == "float"  # #166
    assert _mapped("skill_poise_damage.pvp.projectile_hits")["type"] == "float"
    assert _mapped("skill_poise_damage.hit_labels")["type"] == "keyword"  # #167
    assert _mapped("skill_poise_damage.projectile_hit_counts")["type"] == "integer"
    assert _mapped("skill_poise_damage.projectile_hit_labels")["type"] == "keyword"
    assert _PROPS["ar_inputs"] == {"type": "object", "enabled": False}  # #120
    assert _PROPS["placements"] == {"type": "object", "enabled": False}  # #76


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
            "skill_poise_damage": {
                "max": 30.0,
                "hits": [15.0, 5.0, 30.0, 10.0],
                "hit_labels": ["FP light", "no FP light", "FP heavy", "no FP heavy"],
                "projectile_hits": [5.0],
                "projectile_hit_counts": [4],
                "projectile_hit_labels": ["FP"],
                "pvp": {
                    "max": 810.0,
                    "hits": [405.0, 135.0, 810.0, 270.0],
                    "projectile_hits": [135.0],
                },
            },
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
            "grabs": {
                "count": 1,
                "damage_types": ["Standard"],
                "elements": ["physical", "fire"],
                "attack_power": {"physical": 300, "fire": 150},
                "status_buildup": {"bleed": 70},
                "status_effects": ["bleed"],
            },
            "critical_hits": {
                "backstab": True,
                "riposte": True,
                "stance_break": True,
                "downed": False,
                "sleep": True,
                "other_throw_types": [23],
            },
            "team_type": 7,
            "team": "boss",
            "ai": {
                "think_id": 46300000,
                "sight_distance": 17,
                "sight_angle_width": 60,
                "sight_angle_height": 40,
                "smell_distance": 8,
                "hearing_level": 128,
                "leash_distance": 150,
                "team_attack_weight": 0,
                "guards": False,
            },
            "stats_scaled": {
                "hp": {"min": 1391, "max": 5460, "placements": 4},
                "stamina": {"min": 50, "max": 101},
                "defense": {"magic": {"min": 100, "max": 123}},
                "resistances": {"bleed": {"min": 308, "max": 421}},
                "ng_plus": {
                    "hp": {"min": [1391, 1530], "max": [5460, 6006]},
                    "stamina": {"min": [50, 55], "max": [106, 116]},
                    "defense": {"magic": {"min": [100, 102], "max": [147, 151]}},
                    "resistances": {"bleed": {"min": [308, 312], "max": [421, 427]}},
                },
            },
            "variants": [
                {
                    "npc_ids": ["904650600"],
                    "npc_param_ids": [46500010],
                    "stats": {"hp": 1396, "stamina": 50, "poise": 120.0},
                    "defense": {"magic": 100, "holy": 100},
                    "resistances": {"scarlet_rot": 252, "madness": 999},
                    "immune_to": ["madness"],
                    "traits": [],
                    "weak_point_damage_multiplier": 1.0,
                    "stats_scaled": {
                        "hp": {"min": 5758, "max": 5758, "placements": 1},
                        "stamina": {"min": 101, "max": 101},
                        "resistances": {"madness": {"min": 999, "max": 999}},
                        "ng_plus": {"hp": {"min": [5844], "max": [5844]}},
                    },
                    "maps": ["m12_02_00_00"],
                },
                {
                    "npc_ids": ["904650601"],
                    "npc_param_ids": [46500020],
                    "stats": {"hp": 1396},
                    "immune_to": ["scarlet_rot"],
                },
            ],
            "phases": [
                {
                    "phase": 1,
                    "name": "Godfrey, First Elden Lord",
                    "npc_id": "904720000",
                    "npc_param_id": 47200070,
                    "stats": {"hp": 1721, "stamina": 50, "poise": 120.0},
                    "defense": {"magic": 100},
                    "resistances": {"poison": 542},
                    "immune_to": [],
                    "traits": [],
                    "weak_point_damage_multiplier": 1.0,
                    "stats_scaled": {"hp": 11831},
                    "ends_at_hp_ratio": 0.0,
                    "hp_pool_shared_with": "Hoarah Loux, Warrior",
                },
                {
                    "phase": 2,
                    "name": "Malenia, Goddess of Rot",
                    "npc_id": "902120001",
                    "stats_scaled": {
                        "hp": 18473,
                        "stamina": 101,
                        "defense": {"holy": 123},
                        "resistances": {"bleed": 421},
                        "ng_plus": {
                            "hp": [18750, 20625, 21562, 22500, 24375, 25312, 26250],
                            "stamina": [106, 116, 119, 127, 129, 132, 135],
                            "defense": {"holy": [147, 151, 155, 162, 170, 177, 192]},
                            "resistances": {
                                "bleed": [421, 427, 434, 440, 446, 452, 459]
                            },
                        },
                    },
                    "heals_on_entry": True,
                },
            ],
            "equipment": {
                "weapons": ["Great Stars", "Clawmark Seal"],
                "ashes_of_war": ["Ash of War: Lion's Claw"],
                "armor": ["Page Hood"],
                "spells": ["Beast Claw"],
                "talismans": ["Sacred Scorpion Charm"],
                "ammo": ["Arrow"],
            },
            "weapon_attacks": [
                {
                    "weapon": "Reduvia",
                    "reinforce_level": 10,
                    "hands": ["right", "left"],
                    "damage_types": ["Slash", "Pierce"],
                    "attack_power": {"physical": 124},
                    "status_buildup": {"bleed": 65},
                    "poise_damage": 19.8,
                    "poise_damage_by_attack": {"one_handed": {"r1": 6.6}},
                }
            ],
            "spell_attacks": [
                {
                    "spell": "Frenzied Burst",
                    "damage_types": ["Standard"],
                    "elements": ["fire"],
                    "attack_power": {"fire": 309},
                    "poise_damage": 12.02,
                    "status_buildup": {"madness": 105},
                    "status_effects": ["madness"],
                },
                {
                    "magic_id": 2050090,  # an unnamed NPC-only row (#268)
                    "damage_types": ["Standard"],
                    "elements": ["physical"],
                    "attack_power": {"physical": 190},
                    "poise_damage": 14.0,
                },
            ],
        },
        {"equipped_by": ["Recusant Henricus"]},
        {
            "effect": "+15% attack for 80s; -10% damage taken for 80s",
            "effect_value": 15.0,
            "effect_duration": 80.0,
            "effects": [
                {
                    "stat": "attack",
                    "value": 15.0,
                    "unit": "%",
                    "pvp_value": 7.5,
                    "duration": 80.0,
                },
                {
                    "stat": "HP restored",
                    "value": 8.0,
                    "unit": "points",
                    "interval": 1.0,
                    "condition": "at full HP",
                    "target": "torrent",
                    "scales_with": "faith",
                },
                {"stat": "poison cured", "target": "enemy"},
                # Stacking talisman tier (#155): tiers ride in condition.
                {
                    "stat": "attack",
                    "value": 3.0,
                    "unit": "%",
                    "condition": "successive attacks, tier 1",
                    "target": "self",
                },
            ],
        },
        {
            "acquisition_types": ["found_in_world", "given_by_npc", "keepsake"],
            "given_by": ["Roderika"],
            "starting_classes": ["Vagabond"],
        },
        {
            "entity_type": "boss",
            "name": "Night's Cavalry (Gate Town Bridge)",
            "enemies": ["Night's Cavalry"],
            "location": None,
            "region": "Liurnia of the Lakes",
            "nearest_grace": "Gate Town Bridge",
            "map": "m60_39_43_00",
            "arena_position": {"x": -125.3, "y": 21.5, "z": 34.1},
            "runes": 5600,
            "banner": "Enemy Felled",
            "defeat_flag": 1039430340,
            "drops": ["Ash of War: Ice Spear"],
        },
        {"boss_encounters": ["Godrick the Grafted"]},
        {
            "npc_summons": [
                {
                    "npc": "Nepheli Loux, Warrior",
                    "npc_id": 533340014,
                    "sign": "npc_white",
                    "requires_flag": 10009709,
                },
                {
                    "npc": "Lionel the Lionhearted",
                    "npc_id": 533290040,
                    "sign": "festival",
                },
            ]
        },
        {
            "summonable_for": ["Bloodhound Knight Darriwil", "Starscourge Radahn"],
            "hostile_signs": [
                {
                    "kind": "invasion",
                    "map": "m60_43_37_00",
                    "sign_type": 21,
                    "requires_flag": 1043372740,
                },
                {"kind": "duel", "map": "m60_47_41_00", "sign_type": 2},
            ],
        },
        {
            "entity_type": "site_of_grace",
            "name": "Elden Throne (Leyndell, Ashen Capital)",
            "region": "Leyndell, Ashen Capital",
            "parent_region": "Leyndell, Royal Capital",
            "map": "m11_05_00_00",
            "position": {"x": 37.18, "y": 64.98, "z": -415.93},
            "entity_id": 11051950,
            "unlock_flag": 71120,
            "bosses": ["Hoarah Loux, Warrior"],
        },
        {"world_position": {"x": 10517.3, "y": 41.2, "z": 9828.7}},
        {
            "entity_type": "location",
            "name": "Caelid",
            "kind": "region",
            "graces": ["Smoldering Church"],
            "bosses": ["Starscourge Radahn"],
            "area_scaling": [
                {
                    "speffect_id": 7070,
                    "placements": 1052,
                    "hp": 2.406,
                    "stamina": 1.288,
                    "attack": 1.831,
                    "defense": 1.093,
                    "resistance": 2.123,
                }
            ],
        },
        {
            "placements": [
                {
                    "map": "m60_42_36_00",
                    "world_position": {"x": 10760.1, "y": 60.2, "z": 9310.5},
                    "entity_id": 1042360800,
                    "region": "Stormhill",
                    "parent_region": "Limgrave",
                },
                {
                    "map": "m10_00_00_00",
                    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                    "lot_id": 10000,
                    "in_chest": True,
                    "region": "Stormveil Castle",
                    "location": "Stormveil Castle",
                },
            ],
            "maps": ["m60_42_36_00", "m10_00_00_00"],
            "regions": ["Stormhill", "Limgrave", "Stormveil Castle"],
            "areas": ["Stormhill"],
            "locations": ["Stormveil Castle"],
            "drop_regions": ["Liurnia of the Lakes"],
            "drop_locations": ["Cliffbottom Catacombs"],
        },
        {
            "base_item": None,
            "variants": [
                {
                    "name": "Heavy Halberd",
                    "affinity": "Heavy",
                    "differs": ["attack_power", "guard", "max_level", "scaling"],
                    "attack_power": {"physical": 114, "stamina": 61},
                    "scaling": {"str": {"grade": "B", "value": 97.2}},
                    "status_buildup": {"bleed": 45},
                },
                {
                    "name": "Erdtree's Favor +2",
                    "rank": 2,
                    "differs": ["effect_value", "is_legendary", "weight"],
                    "text_differs": True,
                    "effect_value": 1.065,
                    "weight": 1.5,
                    "is_legendary": True,
                    "availability": "cut",
                },
            ],
        },
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
                    "attacks": {
                        "behavior_variation": 53100,
                        "count": 68,
                        "damage_types": ["Slash", "Pierce"],
                        "elements": ["physical", "holy"],
                        "attack_power": {"physical": 140, "holy": 180},
                        "status_buildup": {"bleed": 50, "madness": 105},
                        "status_effects": ["bleed", "madness"],
                    },
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
                        "attacks": {"attack_power": {"physical": 531, "holy": 683}},
                    }
                ],
            },
            "upgrade_curve": {
                "summon_stats": [
                    {
                        "stats": {"hp": [500, 3711]},
                        "attacks": {"attack_power": {"physical": [140, 531]}},
                    }
                ]
            },
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


def test_effects_note_documents_stacking_tiers():
    # #155: stacking talismans decode, one effects entry per tier.
    note = _FIELD_NOTES["effects"]
    assert "successive attacks, tier 1" in note and "on hit" in note
    assert "stacking talismans" not in note


def test_acquisition_types_note_lists_interaction_reward():
    # #147: interaction-only scripted awards get their own type.
    note = _FIELD_NOTES["acquisition_types"]
    assert "interaction_reward" in note and "Great Rune" in note
    assert "carry neither" not in note
    assert _PROPS["acquisition_types"]["type"] == "keyword"


def test_acquisition_types_note_lists_strike_reward():
    # #266: awards for striking a character get their own type, not interaction.
    note = _FIELD_NOTES["acquisition_types"]
    assert "strike_reward (a map event script awards it for striking" in note
    assert "strike_reward" in mcp_server.search_entities.__doc__


def test_corpse_acquisition_type_and_placement_flag_documented():
    # #136: corpse type + placements[].on_corpse (placements isn't indexed).
    assert "corpse (looted from a body" in _FIELD_NOTES["acquisition_types"]
    assert "on_corpse" in _FIELD_NOTES["placements"]
    assert _PROPS["placements"] == {"type": "object", "enabled": False}


def test_critical_hits_notes_name_the_confirmed_kinds():
    # #128: ThrowParam ThrowType 1 / 20 / 25 labelled; the rest stay raw.
    assert "Tree Sentinel" in _FIELD_NOTES["critical_hits"]
    for leaf in ("backstab", "riposte", "stance_break", "other_throw_types"):
        assert f"critical_hits.{leaf}" in _FIELD_NOTES
    # Multi-phase bosses: per-phase values, top level is their union (Rennala).
    assert "phases.critical_hits" in _FIELD_NOTES
    assert "union" in _FIELD_NOTES["critical_hits"]
