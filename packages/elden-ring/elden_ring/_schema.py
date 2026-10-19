"""The OpenSearch index schema: name, mapping and kuromoji user dictionary (#470).

``INDEX_MAPPING`` is ``dynamic: strict``: every new field needs an entry here (and
a note in ``_field_notes``) before any load. A type or analyzer change needs a new
concrete index; see the package README.
"""

from __future__ import annotations

import os

from elden_ring._calc import DAMAGE_TYPES as _DAMAGE_TYPES
from elden_ring._calc import STATS

# ---------------------------------------------------------------------------
# Index definition
# ---------------------------------------------------------------------------

# Overridable so a full reindex can target a new versioned index before cutover.
INDEX = os.environ.get("ELDEN_RING_INDEX", "elden-ring-entities")

_NEGATION_TYPES = (*_DAMAGE_TYPES, "strike", "slash", "pierce")
_STATUSES = (
    "poison",
    "scarlet_rot",
    "bleed",
    "frostbite",
    "sleep",
    "madness",
    "death_blight",
)
# Follow-up actions of skill.attacks.recovery_frames (#431) and
# skill.frames.recovery (#477).
_SKILL_RECOVERY_ACTIONS = (
    "dodge",
    "right_hand_attack",
    "left_hand_attack",
    "skill",
    "guard",
    "quick_item",
    "item",
    "move",
)


def _props(type_: str, keys) -> dict:
    return {k: {"type": type_} for k in keys}


# Shared mapping fragments (#459): one definition per repeated block, so an
# analyzer or damage-type change is made in one place.
# Japanese text with the three kuromoji sub-fields (#53).
_JA_TEXT = {
    "type": "text",
    "fields": {
        "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
        "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
        "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
    },
}
# Integer per damage type: attack power, and motion values.
_AP_INT = {"properties": _props("integer", _DAMAGE_TYPES)}
# Float negation / damage multiplier per damage and physical subtype.
_NEGATION = {"properties": _props("float", _NEGATION_TYPES)}

# A raw event flag's quest step (#189): summon-sign requires_step, cutscene
# trigger_steps.
_QUEST_LINK = {
    **_props("keyword", ("quest", "npc", "life_state")),
    "phase_flag": {"type": "long"},
    "order": {"type": "integer"},
}

# One end of a warp doc (#94): from / to.
_WARP_END = {
    **_props("keyword", ("map", "grace", "region", "parent_region", "location")),
    "entity_id": {"type": "long"},
    "world_position": {"type": "object", "enabled": False},
    "position": {"type": "object", "enabled": False},
}


# Weapon stat groups (#115), shared by the +0 fields and max_level (#112). Plain
# `object` fields index as dotted paths (attack_power.fire), so filters, sorts and
# aggregations work as on flat fields.
_WEAPON_STATS = {
    "attack_power": {
        "properties": _props("integer", (*_DAMAGE_TYPES, "stamina", "critical"))
    },
    # Weapon block stats (#114); floats, as the affinity products are
    # fractional (Fire Halberd guard 52.25).
    "guard": {
        "properties": {
            **_props("float", (*_DAMAGE_TYPES, "boost")),
            "resistances": {"properties": _props("float", _STATUSES)},
        }
    },
    "scaling": {
        "properties": {
            s: {
                "properties": {
                    "grade": {"type": "keyword"},
                    "value": {"type": "float"},
                }
            }
            for s in STATS
        }
    },
    # On-hit status buildup (#118).
    "status_buildup": {"properties": _props("integer", _STATUSES)},
}

# Weapon poise damage per attack, by hand (#119); more attacks + powerstance (#122).
_POISE_ATTACKS = (
    "light",
    "heavy",
    "charged_heavy",
    "guard_counter",
    "running_light",
    "running_heavy",
    "rolling_light",
    "crouch_light",
    "jumping_light",
    "jumping_heavy",
    "powerstance",
    # One-handed only (#126).
    "off_hand_light",
    "mounted_light",
    "mounted_heavy",
    "mounted_charged_heavy",
    "mounted_jumping_light",
    "mounted_jumping_heavy",
    "mounted_left_light",
    "mounted_left_heavy",
    "mounted_left_charged_heavy",
    "mounted_left_jumping_light",
    "mounted_left_jumping_heavy",
    "powerstance_running",
    "powerstance_rolling",
    "powerstance_backstep",
    "powerstance_jumping",
)
_POISE_HANDS = {
    hand: {"properties": _props("float", _POISE_ATTACKS)}
    for hand in ("one_handed", "two_handed")
}

# Player criticals from ThrowParam (#128, #240): enemy docs and their phases[] entries.
_CRITICAL_HITS = {
    "backstab": {"type": "boolean"},
    "riposte": {"type": "boolean"},
    "stance_break": {"type": "boolean"},
    "downed": {"type": "boolean"},
    "sleep": {"type": "boolean"},
    "other_throw_types": {"type": "integer"},
}

# In-game enemy stats over MSB placements, one stats_scaled group (#108, #131): HP /
# stamina min-max, the per-key min-max of area-scaled defenses and resistances, and
# the same stats for NG+1..NG+7 in one ng_plus group (#261), plus the kill's rune
# yield (#299).
_MIN_MAX = {"properties": _props("integer", ("min", "max"))}
_STAT_RANGES = {
    "stamina": _MIN_MAX,
    "runes": _MIN_MAX,
    "defense": {"properties": dict.fromkeys(_DAMAGE_TYPES[1:], _MIN_MAX)},
    "resistances": {"properties": dict.fromkeys(_STATUSES, _MIN_MAX)},
}
_NPC_SCALED_RANGES = {
    "stats_scaled": {
        "properties": {
            "hp": {"properties": _props("integer", ("min", "max", "placements"))},
            **_STAT_RANGES,
            "ng_plus": {"properties": {"hp": _MIN_MAX, **_STAT_RANGES}},
        }
    }
}
# The same for one placed row (multi-phase boss phases, #132).
_STAT_VALUES = {
    "hp": {"type": "integer"},
    "stamina": {"type": "integer"},
    "runes": {"type": "integer"},
    "defense": {"properties": _props("integer", _DAMAGE_TYPES[1:])},
    "resistances": {"properties": _props("integer", _STATUSES)},
}
_NPC_SCALED = {
    "stats_scaled": {
        "properties": {**_STAT_VALUES, "ng_plus": {"properties": _STAT_VALUES}}
    }
}

# NpcParam stat groups (#84), shared by enemy docs and spirit-ash summon_stats (#86).
_NPC_STATS = {
    "stats": {
        "properties": {
            "hp": {"type": "integer"},
            "stamina": {"type": "integer"},
            "poise": {"type": "float"},
        }
    },
    "defense": {"properties": _props("float", _DAMAGE_TYPES[1:])},
    "resistances": {"properties": _props("integer", _STATUSES)},
    "immune_to": {"type": "keyword"},
    "traits": {"type": "keyword"},
    "weak_point_damage_multiplier": {"type": "float"},
}

# Enemy-only NpcParam combat fields (#302): poise regeneration delay, resident poise
# damage taken rate, per-proc resistance escalation (ResistCorrectParam). On enemy
# docs, variants and phases; not on summon_stats.
_NPC_COMBAT = {
    "poise_regen_delay": {"type": "float"},
    "poise_damage_taken_multiplier": {"type": "float"},
    "resistance_increase_per_proc": {"properties": _props("integer", _STATUSES)},
    "resistance_multiplier_per_proc": {"properties": _props("float", _STATUSES)},
    # Scripted damage-taken states (#323): resident slot SpEffect damage multipliers
    # kept out of negation, with what lifts them, plus the same row's status buildup
    # and poise damage multipliers under the summon names (#380).
    "scripted_damage_taken": {
        "properties": {
            "speffect_id": {"type": "integer"},
            "multiplier": _NEGATION,
            "status_buildup_taken_multiplier": {
                "properties": _props("float", _STATUSES)
            },
            "poise_damage_taken_multiplier": {"type": "float"},
            "lifted_when": {"type": "keyword"},
            "lifted_by": {
                "properties": {
                    "speffect_ids": {"type": "integer"},
                    "animation_ids": {"type": "integer"},
                    "event_ids": {"type": "integer"},
                }
            },
        }
    },
}

# Attack profile over an NpcParam row's move table (#81), shared by enemy docs and
# spirit-ash summon_stats (#124).
_NPC_ATTACKS = {
    "behavior_variation": {"type": "integer"},
    "count": {"type": "integer"},
    "damage_types": {"type": "keyword"},
    "elements": {"type": "keyword"},
    "attack_power": _AP_INT,
    # Max per-hit AtkParam_Npc poise damage (#253); spirits' x0.05 is folded in.
    "poise_damage": {"type": "float"},
    "status_buildup": {"properties": _props("integer", _STATUSES)},
    "status_effects": {"type": "keyword"},
}

# One spirit-ash summon entry (#86) at a given upgrade level (#117).
_SUMMON_STATS = {
    # The spirit's NpcParam row and its drawn mesh parts (ModelDisplayMask bits, #396)
    "npc_param_id": {"type": "integer"},
    "model_display_masks": {"type": "integer"},
    # What a spirit with a same-stats twin wields, read from its mask (#418)
    "weapons": {"type": "keyword"},
    "count": {"type": "integer"},
    **_NPC_STATS,
    "damage_multiplier": {"type": "float"},
    # damage_multiplier x the resident vs-enemy corrections common to every element (#254)
    "damage_vs_enemies_multiplier": {"type": "float"},
    # Resident NpcParam slot SpEffects' damage cut rates (#181) x the vs-enemy damage
    # correction (#248); poise damage / status buildup received rates (#249)
    "damage_taken_multiplier": _NEGATION,
    "poise_damage_taken_multiplier": {"type": "float"},
    "status_buildup_taken_multiplier": {"properties": _props("float", _STATUSES)},
    "attacks": {"properties": _NPC_ATTACKS},
}


# One condition of a quest step / outcome (#95).
_QUEST_CONDITION = {
    "kind": {"type": "keyword"},
    "flag": {"type": "long"},
    "negated": {"type": "boolean"},
    "npc": {"type": "keyword"},
    "npcs": {"type": "keyword"},
    "quest": {"type": "keyword"},
    "life_state": {"type": "keyword"},
    "bosses": {"type": "keyword"},
    "items": {"type": "keyword"},
    "item_id": {"type": "long"},
    "set_at": {"type": "keyword"},
    # kind hit_count (#218): the character's MSB entities + the hits counted
    "entity_ids": {"type": "long"},
    "hits": {"type": "integer"},
    # kind quest_flag (#348): a common close-out also turns the flag on once any of
    # these (leaf conditions) holds; stored, not indexed (the field limit)
    "also_set_when": {"type": "object", "enabled": False},
    # kind flag (#314): the conditions that turn the flag on (as a set_when);
    # stored, not indexed (the field limit)
    "set_when": {"type": "object", "enabled": False},
}
_QUEST_CONDITION_LEAF = _QUEST_CONDITION
# kind any_of (#203): at least one of the nested conditions holds; a member is a
# leaf or kind all_of (#484: every one of its own conditions holds). An all_of
# member may be an any_of of leaves (#494, the third level), stored, not indexed:
# no 1.17 quest step or outcome holds one, and the field limit
_QUEST_CONDITION = {
    **_QUEST_CONDITION,
    "conditions": {
        "properties": {
            **_QUEST_CONDITION,
            "conditions": {
                "properties": {
                    **_QUEST_CONDITION,
                    "conditions": {"type": "object", "enabled": False},
                }
            },
        }
    },
}
# What an 'event' outcome's script waited for (#213): the conditions above, or kind
# character (a character's state, named via its MSB entities); multiplayer_state (#245)
_QUEST_WAITED_FOR = {
    **_QUEST_CONDITION,
    "state": {"type": "keyword"},
    # state special_effect: the SpEffectParam row waited for (#251)
    "special_effect_id": {"type": "long"},
}
# One condition of a world-state flag's set_when (#228): the above, plus the
# decoded progress waits (#231) and, inside any_of, all_of (#233; one level).
_SET_WHEN_LEAF = {
    **{k: v for k, v in _QUEST_WAITED_FOR.items() if k != "conditions"},
    # kind action_button: the prompt pressed, on an MSB entity
    "action_button_id": {"type": "long"},
    "prompt": {"type": "keyword"},
    "entity_id": {"type": "long"},
    # kind in_region: the player in an MSB region (entity_id) on a map
    "map": {"type": "keyword"},
    "locations": {"type": "keyword"},
    "area": {"type": "keyword"},
    "subarea": {"type": "keyword"},
    "landmark": {"type": "keyword"},
    # kind flag_range: IfFlagRangeState over [first_flag, last_flag]
    "first_flag": {"type": "long"},
    "last_flag": {"type": "long"},
    "range_state": {"type": "keyword"},
    # kind flag_count (#486): how many flags of [first_flag, last_flag] are on,
    # compared with count; counted names what the flags record when known
    "comparison": {"type": "keyword"},
    "count": {"type": "integer"},
    "counted": {"type": "keyword"},
    # kind near_entity (#234): the player within distance of MSB entity_id
    "distance": {"type": "float"},
    # kind enemy_killed (#202): the NpcParam rows whose death sets the flag
    "npc_param_ids": {"type": "integer"},
}
_SET_WHEN = {
    **_SET_WHEN_LEAF,
    "conditions": {
        "properties": {
            **_SET_WHEN_LEAF,
            "conditions": {"properties": _SET_WHEN_LEAF},
        }
    },
}

# Kuromoji user dictionary (#53): game coinages IPADIC lacks, kept as one token
# instead of fragments (霊/体), wrong words (輝/石頭) or misread verbs (遺灰 →
# 遺る/灰, しろがね → する/が/ね). Picked from the 1.17.1 corpus: lore terms in
# >= ~20 texts. Descriptive compounds (大剣, 赤獅子) keep their split so 剣
# still matches 大剣. Rows are "surface,segmentation,reading,POS"; the custom
# POS isn't in kuromoji_part_of_speech's stoptags, so .ja keeps these tokens.
# User-dict tokens carry no base form, so this can't fix verb lemmas (模す).
# Changing the rules needs a new concrete index (tokenizer settings are static).
KUROMOJI_USER_DICTIONARY = [
    f"{surface},{surface},{reading},{pos}"
    for surface, reading, pos in [
        ("褪せ人", "アセビト", "カスタム名詞"),
        ("黄金樹", "オウゴンジュ", "カスタム名詞"),
        ("黄金律", "オウゴンリツ", "カスタム名詞"),
        ("二本指", "ニホンユビ", "カスタム名詞"),
        ("三本指", "サンボンユビ", "カスタム名詞"),
        ("結晶人", "ケッショウジン", "カスタム名詞"),
        ("しろがね", "シロガネ", "カスタム名詞"),
        ("しろがね人", "シロガネビト", "カスタム名詞"),
        ("遺灰", "イハイ", "カスタム名詞"),
        ("戦灰", "センハイ", "カスタム名詞"),
        ("戦技", "センギ", "カスタム名詞"),
        ("霊体", "レイタイ", "カスタム名詞"),
        ("霊姿", "レイシ", "カスタム名詞"),
        ("霊馬", "レイバ", "カスタム名詞"),
        ("霊炎", "レイエン", "カスタム名詞"),
        ("聖杯瓶", "セイハイビン", "カスタム名詞"),
        ("鈴玉", "スズダマ", "カスタム名詞"),
        ("古竜", "コリュウ", "カスタム名詞"),
        ("調香師", "チョウコウシ", "カスタム名詞"),
        ("狂い火", "クルイビ", "カスタム名詞"),
        ("朱い", "アカイ", "カスタム形容詞"),
        ("デミゴッド", "デミゴッド", "カスタム名詞"),
        ("影樹", "エイジュ", "カスタム名詞"),
        ("幻影樹", "ゲンエイジュ", "カスタム名詞"),  # else 幻/影樹
        ("聖樹", "セイジュ", "カスタム名詞"),
        ("角人", "ツノビト", "カスタム名詞"),
        ("鍛石", "タンセキ", "カスタム名詞"),
        ("竜餐", "リュウサン", "カスタム名詞"),
        ("神肌", "カミハダ", "カスタム名詞"),
        ("指読み", "ユビヨミ", "カスタム名詞"),
        ("卑兵", "ヒヘイ", "カスタム名詞"),
        ("鉤指", "カギユビ", "カスタム名詞"),
        ("根脂", "ネアブラ", "カスタム名詞"),
        ("亜人", "アジン", "カスタム名詞"),
        ("忌み子", "イミコ", "カスタム名詞"),
        ("忌み鬼", "イミオニ", "カスタム名詞"),
        ("祖霊", "ソレイ", "カスタム名詞"),
        ("黒炎", "コクエン", "カスタム名詞"),
        ("封牢", "フウロウ", "カスタム名詞"),
        ("神授塔", "シンジュトウ", "カスタム名詞"),
        ("夜騎兵", "ヨルキヘイ", "カスタム名詞"),
        ("緋雫", "ヒシズク", "カスタム名詞"),
        ("青雫", "アオシズク", "カスタム名詞"),
        ("背律者", "ハイリツシャ", "カスタム名詞"),
        ("指巫女", "ユビミコ", "カスタム名詞"),
        ("瘤脂", "コブアブラ", "カスタム名詞"),
        ("調霊", "チョウレイ", "カスタム名詞"),
        ("死衾", "シフスマ", "カスタム名詞"),
        ("輝剣", "キケン", "カスタム名詞"),
        ("百智卿", "ヒャクチキョウ", "カスタム名詞"),
        ("輝石頭", "キセキアタマ", "カスタム名詞"),
        ("死王子", "シオウジ", "カスタム名詞"),
        ("指様", "ユビサマ", "カスタム名詞"),
        ("指痕", "ユビアト", "カスタム名詞"),
        ("混種", "コンシュ", "カスタム名詞"),
        # mid-frequency coinages, ~10-30 texts each (#272)
        ("稀人", "マレビト", "カスタム名詞"),
        ("聖痕", "セイコン", "カスタム名詞"),
        ("呪痕", "ジュコン", "カスタム名詞"),
        ("竜人", "リュウジン", "カスタム名詞"),
        ("竜印", "リュウイン", "カスタム名詞"),
        ("獣爪", "ジュウソウ", "カスタム名詞"),
        ("結晶雫", "ケッショウシズク", "カスタム名詞"),
        ("霊灰", "レイハイ", "カスタム名詞"),
        ("責問官", "セキモンカン", "カスタム名詞"),
    ]
]

# One decoded SpEffect entry (#88): item ``effects`` and ``skill.effects`` (#326).
_EFFECTS_MAPPING = {
    "properties": {
        "stat": {"type": "keyword"},
        "value": {"type": "float"},
        "unit": {"type": "keyword"},
        "pvp_value": {"type": "float"},
        "condition": {"type": "keyword"},
        "interval": {"type": "float"},
        "duration": {"type": "float"},
        "target": {"type": "keyword"},
        "scales_with": {"type": "keyword"},
    }
}

# Skill-level frame data (#477): ``skill.frames`` and
# ``skill.frames_by_class.frames``.
_SKILL_FRAMES_MAPPING = {
    "properties": {
        "startup": {"type": "integer"},
        "invulnerable": {"properties": _props("integer", ("start", "end"))},
        "parry": {"properties": _props("integer", ("start", "end"))},
        "recovery": {"properties": _props("integer", _SKILL_RECOVERY_ACTIONS)},
        "total": {"type": "integer"},
    }
}

# One skill hit (#326): ``skill.attacks`` and ``skill.attacks_by_class.attacks``.
_SKILL_ATTACKS_MAPPING = {
    "properties": {
        "hit_label": {"type": "keyword"},
        "motion_values": _AP_INT,
        "attack_power": _AP_INT,
        "stamina_cost": {"type": "integer"},
        "active_frames": {"properties": _props("integer", ("start", "end"))},
        # Recovery per follow-up action, anim length (#431).
        "recovery_frames": {"properties": _props("integer", _SKILL_RECOVERY_ACTIONS)},
        "total_frames": {"type": "integer"},
        "projectile": {"type": "boolean"},
        "hit_count": {"type": "integer"},
    }
}

# A shield art's other per-hand hit lists (#500): ``skill.attacks_by_hand`` and
# ``skill.attacks_by_class.attacks_by_hand``.
_SKILL_ATTACKS_BY_HAND_MAPPING = {
    "properties": {
        "hands": {"type": "keyword"},
        "attacks": _SKILL_ATTACKS_MAPPING,
    }
}

# A shield art's other per-hand frames (#503): ``skill.frames_by_hand`` and
# ``skill.frames_by_class.frames_by_hand``.
_SKILL_FRAMES_BY_HAND_MAPPING = {
    "properties": {
        "hands": {"type": "keyword"},
        "frames": _SKILL_FRAMES_MAPPING,
    }
}

INDEX_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        # the nested set_when conditions (#231/#233) take the mapping past the
        # default 1000 fields, and renames (#377) leave old leaves in the live
        # index, so 3000; also applied to the live index with put_settings
        "mapping.total_fields.limit": 3000,
        "analysis": {
            "tokenizer": {
                # Kuromoji in normal mode: dictionary-based segmentation with no
                # search-mode decompounding. Compounds like 象牙 stay as one token;
                # 象 never accidentally matches them.
                "kuromoji_normal": {
                    "type": "kuromoji_tokenizer",
                    "mode": "normal",
                    "user_dictionary_rules": KUROMOJI_USER_DICTIONARY,
                },
                # The built-in kuromoji_tokenizer (search mode) plus the same user
                # dictionary, for the .ja relevance analyzer.
                "kuromoji_search": {
                    "type": "kuromoji_tokenizer",
                    "mode": "search",
                    "user_dictionary_rules": KUROMOJI_USER_DICTIONARY,
                },
            },
            "analyzer": {
                # Relevance analyzer for .ja subfields used by search(). Full filter
                # chain: baseform lemmatization, stopword removal, stemming.
                "kuromoji_analyzer": {
                    "type": "custom",
                    "tokenizer": "kuromoji_search",
                    "filter": [
                        "kuromoji_baseform",
                        "kuromoji_part_of_speech",
                        "ja_stop",
                        "lowercase",
                        "kuromoji_stemmer",
                    ],
                },
                # Segmentation-only analyzer for .morph subfields used by
                # search_literal(use_kuromoji=True). No filters: tokens are raw
                # morphemes so phrase queries respect dictionary word boundaries
                # without lemmatization, stopword removal, or stemming.
                "kuromoji_segmenter": {
                    "type": "custom",
                    "tokenizer": "kuromoji_normal",
                },
                # Lemmatizing analyzer for .lemma subfields used by
                # search_literal(use_lemmatize=True). Applies kuromoji_baseform to
                # convert each token to its dictionary form; no stopword/POS removal
                # so phrase queries don't break at particle/stopword boundaries.
                "kuromoji_lemmatizer": {
                    "type": "custom",
                    "tokenizer": "kuromoji_normal",
                    "filter": ["kuromoji_baseform"],
                },
            },
            "normalizer": {
                # ASCII-folding normalizer for diacritic-insensitive name lookup.
                # Powers name.folded subfield used by get_entity() fallback.
                "ascii_normalizer": {
                    "type": "custom",
                    "filter": ["asciifolding", "lowercase"],
                }
            },
        },
    },
    "mappings": {
        # An unmapped field fails the load instead of silently becoming text+keyword.
        "dynamic": "strict",
        "properties": {
            "entity_type": {"type": "keyword"},
            # "<entity_type>::<name>", stamped at load (#443): the collapse, dedupe
            # and distinct-count key, so same-name docs of two types stay apart.
            "entity_key": {"type": "keyword"},
            "name": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "folded": {"type": "keyword", "normalizer": "ascii_normalizer"},
                },
            },
            "display_name": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "folded": {"type": "keyword", "normalizer": "ascii_normalizer"},
                },
            },
            "patch_version": {"type": "keyword"},
            "source": {"type": "keyword"},
            # cut / unobtainable (#71, #102); absent on obtainable content.
            "availability": {"type": "keyword"},
            # Shadow of the Erdtree content, on every doc (#297).
            "is_dlc": {"type": "boolean"},
            "description": {"type": "text"},
            "text_content": {"type": "text"},
            # Item effect / info FMG lines (#90): WeaponEffect, AccessoryInfo,
            # GoodsInfo(2), ProtectorInfo, WeaponInfo.
            "effect_text": {"type": "text"},
            "info_text": {"type": "text"},
            "tags": {"type": "keyword"},
            "location": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            # Boss docs: landmark (a location doc) vs area (region-map name) (#329).
            "location_kind": {"type": "keyword"},
            # Boss docs: the landmark location's world-map marker kind (#340).
            "location_type": {"type": "keyword"},
            "weight": {"type": "float"},
            # Grouped stat objects (#115); weapon stats at +0.
            **_WEAPON_STATS,
            # Weapon/ammo physical damage type(s), main first (#116).
            "damage_types": {"type": "keyword"},
            # Ammo standard-shot flight + follow-up hits and on-hit statuses, from
            # its Bullet chain (#91).
            "projectile": {
                "properties": {
                    **_props(
                        "float",
                        (
                            "speed",
                            "max_speed",
                            "range",
                            "gravity",
                            "lifetime",
                            "hit_radius",
                        ),
                    ),
                    # Thrown goods: the main hit's stance damage (#308).
                    "poise_damage": {"type": "float"},
                    "follow_up_hits": {"type": "integer"},
                    "follow_up_attack_power": _AP_INT,
                    # Motion-value % per element per distinct follow-up hit (#152).
                    "follow_up_motion_values": {
                        "properties": _props("integer", ("count", *_DAMAGE_TYPES))
                    },
                }
            },
            "status_effects": {"type": "keyword"},
            # Ammo: bow-skill shots fired with it, one per skill x hit row (#151);
            # bows and bow Ashes of War: their skill's shots with the standard
            # ammo, named in `ammo` (#177).
            "skill_shots": {
                "properties": {
                    "ammo": {"type": "keyword"},  # #177
                    "skill": {"type": "keyword"},
                    "hit_label": {"type": "keyword"},
                    "hit_count": {"type": "integer"},
                    "hit_count_max": {"type": "integer"},  # #176
                    "poise_damage": {"type": "float"},
                    "motion_values": _AP_INT,
                    "projectile": {
                        "properties": _props(
                            "float",
                            (
                                "speed",
                                "max_speed",
                                "range",
                                "gravity",
                                "lifetime",
                                "hit_radius",
                            ),
                        )
                    },
                }
            },
            # Per-attack poise damage, first hit; chains stored, not indexed (#119).
            "poise_damage": {
                "properties": {**_POISE_HANDS, "pvp": {"properties": _POISE_HANDS}}
            },
            "poise_damage_chains": {"type": "object", "enabled": False},
            # The default skill's per-hit poise, from its TAE judges (#125);
            # projectile (bullet) hits separately (#166).
            "skill_poise_damage": {
                "properties": {
                    "weapon_class": {"type": "keyword"},
                    "hit_labels": {"type": "keyword"},  # #167
                    "projectile_hit_labels": {"type": "keyword"},  # #171
                    "projectile_hit_counts": {"type": "integer"},  # #168
                    **_props(
                        "float", ("weapon_poise", "max", "hits", "projectile_hits")
                    ),
                    "pvp": {
                        "properties": _props(
                            "float", ("max", "hits", "projectile_hits")
                        )
                    },
                }
            },
            # Weapon stats at its max upgrade (+25, somber +10) in the +0 shape, and
            # the per-level curve as non-indexed arrays (#112); spirit-ash summons
            # at +10 (#117).
            "reinforce_type_id": {"type": "integer"},
            "max_level": {
                "properties": {
                    "level": {"type": "integer"},
                    **_WEAPON_STATS,
                    "spell_scaling": {"type": "integer"},  # #309
                    "summon_count": {"type": "integer"},
                    "summon_stats": {"properties": _SUMMON_STATS},
                }
            },
            "upgrade_curve": {"type": "object", "enabled": False},
            "ar_inputs": {"type": "object", "enabled": False},  # #120
            "requirements": {"properties": _props("integer", STATS)},
            "fp_cost": {"type": "integer"},
            "hp_cost": {"type": "integer"},  # #292
            # Item rarity + sell/hold/store/trade limits (#296).
            "rarity": {"type": "integer"},
            "inventory": {
                "properties": {
                    **_props("integer", ("sell_value", "max_held", "max_stored")),
                    **_props("boolean", ("sellable", "storable", "tradable")),
                }
            },
            "spell_role": {"type": "keyword"},
            # Spell stamina, hold action, cast flags (#293) and schools (#294).
            **_props("integer", ("stamina_cost", "stamina_cost_charged")),
            "fp_cost_continuous": {"type": "integer"},
            "cast_hold": {"type": "keyword"},
            **_props(
                "boolean",
                ("is_horseback_castable", "is_weapon_buff", "is_shield_buff"),
            ),
            "spell_schools": {"type": "keyword"},
            "school_boosted_by": {"type": "keyword"},
            "slots": {"type": "integer"},
            "sort_id": {"type": "integer"},
            # Item param row id + menu icon id; armor's body type 2 icon (#327).
            **_props("integer", ("param_id", "icon_id", "icon_id_body_type_2")),
            "menu_category": {"type": "keyword"},
            # Goods inventory sort group (sortGroupId) within the tab (#308).
            "sub_category": {"type": "keyword"},
            # Consumable usage flags (#308).
            "usage": {
                "properties": _props(
                    "boolean",
                    (
                        "reusable",
                        "horseback",
                        "horseback_only",
                        "ladder",
                        "multiplayer",
                        "offline",
                        "bulk_use",
                    ),
                )
            },
            "npc_id": {"type": "keyword"},
            "name_source": {"type": "keyword"},
            "chr_models": {"type": "keyword"},
            # Enemy (NpcParam) combat stats, from the row bound by health bar / NameID
            # / spirit-ash label (#84).
            **_NPC_STATS,
            **_NPC_COMBAT,
            # A humanoid's own NpcParam poise / resistances, replaced in stats /
            # resistances by its armor loadout (#285).
            "npc_param_values": {
                "properties": {
                    "poise": {"type": "float"},
                    "resistances": {"properties": _props("integer", _STATUSES)},
                }
            },
            # Enemy attack profile over its model family's move table (#81).
            "attacks": {
                "properties": {
                    **_NPC_ATTACKS,
                    "shared_with": {"type": "keyword"},
                    # status_effects + every other_tables[].status_effects (#255).
                    "all_status_effects": {"type": "keyword"},
                    # Attack animations with a TAE get-parried window (#302).
                    "parryable": {"type": "boolean"},
                    **_props("integer", ("parryable_animations", "attack_animations")),
                    # Moves other placed rows add under a TAE gate state (#180).
                    "state_variants": {
                        "properties": {
                            **_NPC_ATTACKS,
                            "special_states": {"type": "integer"},
                            "placements": {"type": "integer"},
                            "npc_param_ids": {"type": "integer"},
                        }
                    },
                    # Placed rows of the name on another move table (#250).
                    "other_tables": {
                        "properties": {
                            **_NPC_ATTACKS,
                            "placements": {"type": "integer"},
                            "npc_param_ids": {"type": "integer"},
                            "shared_with": {"type": "keyword"},
                        }
                    },
                }
            },
            # Grab subset of that move table, joined to ThrowParam (#82).
            "grabs": {
                "properties": {
                    "count": {"type": "integer"},
                    "damage_types": {"type": "keyword"},
                    "elements": {"type": "keyword"},
                    "attack_power": _AP_INT,
                    "poise_damage": {"type": "float"},
                    "status_buildup": {"properties": _props("integer", _STATUSES)},
                    "status_effects": {"type": "keyword"},
                }
            },
            # Player criticals the bound row's model allows, from ThrowParam (#128).
            "critical_hits": {"properties": _CRITICAL_HITS},
            # NpcParam TeamType + NpcThinkParam AI profile via MSB placements (#83).
            "team_type": {"type": "integer"},
            "team": {"type": "keyword"},
            "ai": {
                "properties": {
                    **_props(
                        "integer",
                        (
                            "think_id",
                            "sight_distance",
                            "sight_angle_width",
                            "sight_angle_height",
                            "smell_distance",
                            "hearing_level",
                            "leash_distance",
                            "team_attack_weight",
                        ),
                    ),
                    "guards": {"type": "boolean"},
                }
            },
            # In-game stat ranges over MSB placements, area scaling applied (#108),
            # and NG+ HP (#131).
            **_NPC_SCALED_RANGES,
            # Enemies: distinct stat blocks over a name's health-bar placements (#110).
            # Item bases: one summary entry per variant naming it in base_item (#111).
            "variants": {
                "properties": {
                    "name": {"type": "keyword"},
                    "affinity": {"type": "keyword"},
                    "rank": {"type": "integer"},
                    "differs": {"type": "keyword"},
                    **{
                        k: _WEAPON_STATS[k]
                        for k in ("attack_power", "scaling", "status_buildup")
                    },
                    "weight": {"type": "float"},
                    "effect_value": {"type": "float"},
                    "is_legendary": {"type": "boolean"},
                    "text_differs": {"type": "boolean"},
                    "availability": {"type": "keyword"},
                    "npc_ids": {"type": "keyword"},
                    "npc_param_ids": {"type": "integer"},
                    **_NPC_STATS,
                    **_NPC_COMBAT,
                    "negation": _NEGATION,
                    **_NPC_SCALED_RANGES,
                    "maps": {"type": "keyword"},
                }
            },
            # Multi-phase boss fights: one entry per fighting character (#132).
            "phases": {
                "properties": {
                    "phase": {"type": "integer"},
                    "name": {"type": "keyword"},
                    "npc_id": {"type": "keyword"},
                    "npc_param_id": {"type": "integer"},
                    **_NPC_STATS,
                    **_NPC_COMBAT,
                    "negation": _NEGATION,  # #298
                    **_NPC_SCALED,
                    "ends_at_hp_ratio": {"type": "float"},
                    "hp_pool_shared_with": {"type": "keyword"},
                    "heals_on_entry": {"type": "boolean"},
                    "critical_hits": {"properties": _CRITICAL_HITS},  # #128
                }
            },
            # Boss encounters (GameAreaParam + defeat banner, #79) and the reverse
            # link on enemy docs.
            "enemies": {"type": "keyword"},
            "region": {"type": "keyword"},
            "nearest_grace": {"type": "keyword"},
            "map": {"type": "keyword"},
            "arena_position": {"properties": _props("float", ("x", "y", "z"))},
            "runes": {"type": "integer"},
            # NG+1..NG+7 and multiplayer rune rewards (#299)
            "runes_ng_plus": {"type": "integer"},
            "runes_coop": {"properties": _props("integer", ("host", "cooperator"))},
            "banner": {"type": "keyword"},
            "defeat_flag": {"type": "long"},
            "boss_encounters": {"type": "keyword"},
            # NPC summon signs (#93): npc_summons on boss docs; the reverse link and
            # the boss-less signs (invasions, duels) on enemy docs.
            "npc_summons": {
                "properties": {
                    "npc": {"type": "keyword"},
                    "npc_id": {"type": "integer"},
                    "sign": {"type": "keyword"},
                    "requires_flag": {"type": "long"},
                    "requires_step": {"properties": _QUEST_LINK},  # #189
                    "requires_set_when": {"properties": _SET_WHEN},  # #228
                    # the other npcs whose sign can't be out with this one (#338)
                    "excludes": {"type": "keyword"},
                }
            },
            "summonable_for": {"type": "keyword"},
            "hostile_signs": {
                "properties": {
                    "kind": {"type": "keyword"},
                    "map": {"type": "keyword"},
                    "sign_type": {"type": "integer"},
                    "requires_flag": {"type": "long"},
                    "requires_step": {"properties": _QUEST_LINK},  # #189
                    "requires_set_when": {"properties": _SET_WHEN},  # #228
                }
            },
            # Sites of grace (BonfireWarpParam, #78); region/map/nearest_grace shared
            # with bosses.
            "parent_region": {"type": "keyword"},
            "position": {"properties": _props("float", ("x", "y", "z"))},
            "world_position": {"properties": _props("float", ("x", "y", "z"))},
            "entity_id": {"type": "long"},
            "unlock_flag": {"type": "long"},  # also on tutorial game_text (#202)
            "unlock_set_when": {"properties": _SET_WHEN},  # #202
            "grants_item": {"type": "keyword"},  # tutorial game_text (#384)
            "raised_by_items": {"type": "keyword"},  # item_dialog game_text (#316)
            "message_category": {"type": "keyword"},  # message_word game_text (#387)
            "bosses": {"type": "keyword"},
            # Locations (map-menu regions + WorldMapPointParam markers, #77); name /
            # region / parent_region / map / positions / bosses shared with graces.
            "kind": {"type": "keyword"},
            "graces": {"type": "keyword"},
            "items": {"type": "keyword"},  # items placed there (#315); enemies shared
            "near_items": {"type": "keyword"},  # items just outside it (#476)
            "area_scaling": {
                "properties": {
                    "speffect_id": {"type": "long"},
                    "placements": {"type": "integer"},
                    **_props(
                        "float", ("hp", "stamina", "attack", "defense", "resistance")
                    ),
                }
            },
            # Open-world landmark MSB MapPoint volumes (#80): returned, never searched.
            "footprint": {"type": "object", "enabled": False},
            # NPC-invasion map instances (Ceremony rows) in a location (#96).
            "invasion_instances": {
                "properties": {
                    "ceremony": {"type": "integer"},
                    "hosts": {"type": "keyword"},
                    "invader_flag": {"type": "long"},
                }
            },
            # MSB placements of enemies and treasure pickups of items (#76): the
            # list is returned, never searched; maps is the filterable summary.
            "placements": {"type": "object", "enabled": False},
            "maps": {"type": "keyword"},
            # Placement region / dungeon location summaries (#140).
            "regions": {"type": "keyword"},
            # The placements' region-map areas (#194).
            "areas": {"type": "keyword"},
            "locations": {"type": "keyword"},
            "drop_regions": {"type": "keyword"},
            "drop_locations": {"type": "keyword"},
            "drop_near_locations": {"type": "keyword"},  # #347
            "near_locations": {"type": "keyword"},  # #427
            # Warp / portal graph (EMEVD WarpToMap + cutscene warps, #94): each end
            # is filterable by map / grace / region / location; positions are
            # returned, not searched. warps_to / warps_from summarise it on
            # site_of_grace and location docs.
            "from": {"properties": _WARP_END},
            "to": {"properties": _WARP_END},
            "prompt": {"type": "keyword"},
            "item": {"type": "keyword"},  # an item-use warp (#315)
            "gate_flag": {"type": "long"},
            "gate_set_when": {"properties": _SET_WHEN},  # #228
            "event_id": {"type": "long"},
            "cutscene_id": {"type": "long"},  # also on cutscene docs (#92)
            "warps_to": {"type": "keyword"},
            "warps_from": {"type": "keyword"},
            # Humanoid enemy loadout (MSB CharaInitID -> CharaInitParam, #85) and
            # its reverse on item docs.
            "equipment": {
                "properties": _props(
                    "keyword",
                    (
                        "weapons",
                        "ashes_of_war",
                        "armor",
                        "spells",
                        "talismans",
                        "ammo",
                    ),
                )
            },
            "equipped_by": {"type": "keyword"},
            # Humanoid attacks from the loadout weapons (#127): one entry per weapon
            # row + level + ash; weapon numbers, not AtkParam_Npc absolutes, so kept
            # apart from `attacks`. Per-attack poise is stored, not indexed.
            "weapon_attacks": {
                "properties": {
                    **_props(
                        "keyword",
                        ("weapon", "ash_of_war", "hands", "damage_types", "elements"),
                    ),
                    "reinforce_level": {"type": "integer"},
                    "attack_power": _AP_INT,
                    "status_buildup": {"properties": _props("integer", _STATUSES)},
                    "poise_damage": {"type": "float"},
                    "poise_damage_by_attack": {"type": "object", "enabled": False},
                }
            },
            # Humanoid attacks from the loadout spells (#267): one entry per spell,
            # the `attacks` aggregate over its own Magic row's bullets / AtkParam_Pc
            # rows. Buildup numbers are stored, not indexed (status_effects is).
            # Spell docs carry their own single entry (#130); a hitting loadout row
            # no item name resolves is keyed by magic_id instead of spell (#268).
            "spell_attacks": {
                "properties": {
                    **_props(
                        "keyword",
                        ("spell", "damage_types", "elements", "status_effects"),
                    ),
                    "magic_id": {"type": "integer"},
                    "attack_power": _AP_INT,
                    "poise_damage": {"type": "float"},
                    "status_buildup": {"type": "object", "enabled": False},
                    # hits per cast and their summed power (#270), channelled
                    # spells, and the uncharged / charged casts (#269)
                    "hit_count": {"type": "integer"},
                    "attack_power_per_cast": _AP_INT,
                    "channeled": {"type": "boolean"},
                    "guard_pierce": {"type": "integer"},  # #293
                    "flat": {"type": "boolean"},  # #331 unscaled contact hit
                    "lingering_duration": {"type": "float"},  # #375 pool seconds
                    **{
                        cast: {
                            "properties": {
                                "attack_power": _AP_INT,
                                "poise_damage": {"type": "float"},
                                # per-cast buildup + guard pierce (#293)
                                "status_buildup": {"type": "object", "enabled": False},
                                "guard_pierce": {"type": "integer"},
                                "hit_count": {"type": "integer"},
                                "attack_power_per_cast": _AP_INT,
                                "lingering_duration": {"type": "float"},  # #375
                            }
                        }
                        for cast in ("uncharged", "charged")
                    },
                }
            },
            # Spirit-ash summons: one entry per distinct summoned NpcParam row (#86).
            "summon_count": {"type": "integer"},
            "summon_stats": {"properties": _SUMMON_STATS},
            # Realtime cutscenes (EMEVD plays + MQB subtitles, #92); map / tags
            # shared, and the back-reference on boss docs.
            "variant_ids": {"type": "long"},
            "asset": {"type": "keyword"},
            "label": {"type": "text"},
            "trigger_kind": {"type": "keyword"},
            "boss": {"type": "keyword"},
            "trigger_flags": {"type": "long"},
            "trigger_steps": {"properties": _QUEST_LINK},  # #189
            "trigger_set_when": {  # #228
                "properties": {
                    "flag": {"type": "long"},
                    "when": {"properties": _SET_WHEN},
                }
            },
            "trigger_items": {"type": "keyword"},
            "trigger_options": {"type": "keyword"},  # #334
            "trigger_speffects": {  # #280
                "properties": {
                    "speffect_id": {"type": "long"},
                    "entity_id": {"type": "long"},
                    "character": {"type": "keyword"},
                    "bosses": {"type": "keyword"},
                }
            },
            "trigger_health": {  # #394
                "properties": {
                    "entity_id": {"type": "long"},
                    "character": {"type": "keyword"},
                    "bosses": {"type": "keyword"},
                    "comparison": {"type": "keyword"},
                    "ratio": {"type": "float"},
                }
            },
            "trigger_deaths": {  # #394
                "properties": {
                    "entity_id": {"type": "long"},
                    "character": {"type": "keyword"},
                    "bosses": {"type": "keyword"},
                }
            },
            "warp_region": {"type": "long"},
            "is_ending": {"type": "boolean"},
            "ending": {"type": "keyword"},  # its ending doc (#425)
            # Ending docs (#425); cutscene, boss, npcs and trigger_* shared.
            "ending_flags": {"type": "long"},
            "choices": {"type": "keyword"},
            "unskippable": {"type": "boolean"},
            "subtitles": {"type": "text"},
            "subtitles_ja": _JA_TEXT,
            "talk_ids": {"type": "long"},
            "speakers": {"type": "keyword"},  # #191; npc_dialogue too (#307)
            "talk_contexts": {"type": "keyword"},  # npc_dialogue (#307)
            "requires_flags": {"type": "long"},  # npc_dialogue (#335)
            "requires_steps": {"properties": _QUEST_LINK},  # npc_dialogue (#335)
            "requires_flags_off": {"type": "long"},  # npc_dialogue (#389)
            "requires_steps_off": {"properties": _QUEST_LINK},  # npc_dialogue (#389)
            "npcs": {"type": "keyword"},  # talk_option (#307)
            "sets_flags": {"type": "long"},  # talk_option (#334)
            # Back-link on npc_dialogue subtitle lines (#192); cutscene_id shared.
            "cutscene": {"type": "keyword"},
            "cutscenes": {
                "properties": {
                    "id": {"type": "long"},
                    "kind": {"type": "keyword"},
                }
            },
            # NPC questlines (#95): one doc per NPC event-flag block.
            "npc": {"type": "keyword"},
            "npc_names": {"type": "keyword"},
            "rewards": {"type": "keyword"},  # #304
            "flag_block": {"type": "long"},
            "related_npcs": {"type": "keyword"},
            "steps": {
                "properties": {
                    "phase_flag": {"type": "long"},
                    "order": {"type": "integer"},
                    "entered_from": {"type": "long"},
                    "locations": {"type": "keyword"},
                    "when": {
                        "properties": {
                            **_QUEST_CONDITION,
                            # #512: kind flag_range, a range test over another
                            # NPC's block (top level only)
                            "first_flag": {"type": "long"},
                            "last_flag": {"type": "long"},
                            "range_state": {"type": "keyword"},
                        }
                    },
                    # #485: what bringing the NPC in during the phase needs
                    "placements": {
                        "properties": {
                            "locations": {"type": "keyword"},
                            "when": {"properties": _QUEST_CONDITION},
                            # #495: placed until one of these (leaves) holds
                            "until": {"properties": _QUEST_CONDITION_LEAF},
                        }
                    },
                }
            },
            # #510: the phase DAG as explicit edges, from steps' entered_from
            "phase_edges": {
                "properties": {
                    "from": {"type": "long"},
                    "to": {"type": "long"},
                    "any_of_group": {"type": "integer"},
                }
            },
            "outcomes": {
                "properties": {
                    "flag": {"type": "long"},
                    "slot": {"type": "integer"},
                    "life_state": {"type": "keyword"},
                    "trigger": {"type": "keyword"},
                    "when": {"properties": _QUEST_CONDITION},
                    "waited_for": {"properties": _QUEST_WAITED_FOR},
                }
            },
            "name_ja": _JA_TEXT,
            "description_ja": _JA_TEXT,
            "text_content_ja": _JA_TEXT,
            "effect_text_ja": _JA_TEXT,
            "info_text_ja": _JA_TEXT,
            "acquisition_types": {"type": "keyword"},
            "acquisition_sources": {"type": "keyword"},
            "dropped_by": {"type": "keyword"},
            # Per-killer drop scope (#230): placement vs type-wide, + chance.
            "drop_sources": {
                "properties": {
                    "enemy": {"type": "keyword"},
                    "scope": {"type": "keyword"},
                    "chance": {"type": "float"},
                    # The range when merged variant lots disagree (#275).
                    "chance_min": {"type": "float"},
                    "chance_max": {"type": "float"},
                    # Per-quantity chances when one roll can give more than one (#279).
                    "quantities": {
                        "properties": {
                            "quantity": {"type": "integer"},
                            "chance": {"type": "float"},
                        }
                    },
                }
            },
            "drops": {"type": "keyword"},
            "sold_by": {"type": "keyword"},
            "shop_listings": {"type": "object", "enabled": False},  # #89
            "given_by": {"type": "keyword"},
            "in_exchange_for": {"type": "keyword"},  # #97
            "in_exchange_count": {"type": "integer"},  # #193
            "in_exchange_flag": {"type": "long"},  # #200
            "in_exchange_step": {"properties": _QUEST_LINK},  # #200
            "exchanged_for": {"type": "keyword"},  # #97
            # Scripted awards' source (#304): map, gating flags, NPC, quest step.
            "reward_sources": {
                "properties": {
                    **_props(
                        "keyword",
                        (
                            "type",
                            "map",
                            "npc",
                            "locations",
                            "requires_items",
                            "requires_any_items",  # #368
                        ),
                    ),
                    "flags": {"type": "long"},
                    "step": {"properties": _QUEST_LINK},
                    "requires_interaction": {"type": "boolean"},  # #360
                }
            },
            "from_weapons": {"type": "keyword"},  # #305
            # Paintings (#316): reward <-> painting, and where the reward waits.
            "from_paintings": {"type": "keyword"},
            "painting_rewards": {"type": "keyword"},
            "viewpoints": {"type": "object", "enabled": False},
            "from_tutorial": {"type": "keyword"},  # 'About …' items (#384)
            # Duplication menus (#223): Ash of War / remembrance copies.
            "duplication": {
                "properties": {
                    "service": {"type": "keyword"},
                    "where": {"type": "keyword"},
                    "also_at": {"type": "keyword"},  # #225
                    "only_at_bell_mausoleums": {"type": "boolean"},  # #227
                    "price": {"type": "integer"},
                    "currency": {"type": "keyword"},
                    "quantity": {"type": "integer"},
                    "unlock_flag": {"type": "long"},
                    "unlocked_by_defeating": {"type": "keyword"},
                }
            },
            "starting_classes": {"type": "keyword"},
            # Starting class docs (#422): CharaInitParam level, attributes, flask
            # charges, weapon / ammo slots and starting gestures (plus equipment
            # and items, shared with other docs).
            "level": {"type": "integer"},
            "attributes": {
                "properties": _props(
                    "integer",
                    (
                        "vigor",
                        "mind",
                        "endurance",
                        "strength",
                        "dexterity",
                        "intelligence",
                        "faith",
                        "arcane",
                    ),
                )
            },
            "flasks": {
                "properties": _props("integer", ("crimson_tears", "cerulean_tears"))
            },
            "weapon_slots": {
                "properties": {
                    "slot": {"type": "keyword"},
                    "weapon": {"type": "keyword"},
                    "upgrade_level": {"type": "integer"},
                }
            },
            "ammo_slots": {
                "properties": {
                    "slot": {"type": "keyword"},
                    "ammo": {"type": "keyword"},
                    "quantity": {"type": "integer"},
                }
            },
            "gestures": {"type": "keyword"},
            # Keepsake docs (#475): what the keepsake gives, each item linked by
            # name + entity_type.
            "grants": {
                "properties": {
                    "item": {"type": "keyword"},
                    "entity_type": {"type": "keyword"},
                    "quantity": {"type": "integer"},
                }
            },
            # Great Rune twins: key item <-> restored rune (#312).
            "restoration": {
                "properties": {
                    "location": {"type": "keyword"},
                    "map": {"type": "keyword"},
                    "twin_entity_type": {"type": "keyword"},
                    "twin_param_id": {"type": "integer"},
                    "dropped_by": {"type": "keyword"},
                }
            },
            # Crafting (#87).
            "crafted_from": {
                "properties": {
                    "item": {"type": "keyword"},
                    "quantity": {"type": "integer"},
                }
            },
            "crafted_yield": {"type": "integer"},
            "recipe_unlock": {"type": "keyword"},
            "used_in": {"type": "keyword"},
            # Upgrade material -> the levels it pays for (reverse of
            # upgrade_curve.materials, #311).
            "upgrade_use": {
                "properties": {
                    "target": {"type": "keyword"},
                    "level": {"type": "integer"},
                    "quantity": {"type": "integer"},
                }
            },
            # Whetblades' affinities; per-level blessing / flask tables (#311).
            "unlocks_affinities": {"type": "keyword"},
            "blessing": {"type": "object", "enabled": False},
            "flask_restoration": {"type": "object", "enabled": False},
            # Pot / aromatic -> the tool it is crafted in, by potGroupId (#308).
            "crafting_container": {"type": "keyword"},
            "unlocks_recipes": {"type": "keyword"},
            "unlocks_shop_items": {"type": "keyword"},  # #144
            "base_item": {"type": "keyword"},
            "text_differs": {"type": "boolean"},
            "text_added_lines": {"type": "text"},
            "affinity": {"type": "keyword"},
            "is_legendary": {"type": "boolean"},
            "effect": {"type": "text"},
            "effect_value": {"type": "float"},
            # Structured SpEffect decoding (#88).
            "effects": _EFFECTS_MAPPING,
            "effect_duration": {"type": "float"},
            "infusable": {"type": "boolean"},
            "accepts_ash_of_war": {"type": "boolean"},  # #513
            # Weapon flags / rates, trait multipliers, guard pierce and the staff /
            # seal scaling at its requirements (#309).
            **_props("boolean", ("buffable", "can_guard")),
            **_props("integer", ("bow_range_change", "guard_pierce", "spell_scaling")),
            "stamina_cost_multiplier": {"type": "float"},
            "damage_vs_traits": {
                "properties": _props(
                    "float",
                    ("weak_to_gravity", "lives_in_death", "ancient_dragon", "dragon"),
                )
            },
            "default_ash_of_war": {"type": "keyword"},
            # Weapon / Ash of War skill: FP costs, chargeable, caption (#289).
            "skill": {
                "properties": {
                    "name": {"type": "keyword"},
                    "caption": {"type": "text"},
                    "fp_cost": {"type": "integer"},
                    "follow_up_fp_cost": {
                        "properties": _props("integer", ("light", "heavy"))
                    },
                    "chargeable": {"type": "boolean"},
                    # Per-hit motion values, stamina, frames; the buff (#326).
                    "attacks": _SKILL_ATTACKS_MAPPING,
                    # Shield arts' left-hand / two-handed hit lists (#500).
                    "attacks_by_hand": _SKILL_ATTACKS_BY_HAND_MAPPING,
                    # Ash of War hit lists of the classes that differ (#432).
                    "attacks_by_class": {
                        "properties": {
                            "weapon_classes": {"type": "keyword"},
                            "attacks": _SKILL_ATTACKS_MAPPING,
                            "attacks_by_hand": _SKILL_ATTACKS_BY_HAND_MAPPING,
                        }
                    },
                    # Skill-level i-frames / parry window / recovery (#477).
                    "frames": _SKILL_FRAMES_MAPPING,
                    # Shield arts' left-hand / two-handed frames (#503).
                    "frames_by_hand": _SKILL_FRAMES_BY_HAND_MAPPING,
                    # Ash of War frames of the classes that differ (#477).
                    "frames_by_class": {
                        "properties": {
                            "weapon_classes": {"type": "keyword"},
                            "frames": _SKILL_FRAMES_MAPPING,
                            "frames_by_hand": _SKILL_FRAMES_BY_HAND_MAPPING,
                        }
                    },
                    "effects": _EFFECTS_MAPPING,
                    "effect": {"type": "text"},
                    "effect_value": {"type": "float"},
                    "effect_duration": {"type": "float"},
                    # Ash of War buffs of the classes whose variant differs (#482).
                    "effects_by_class": {
                        "properties": {
                            "weapon_classes": {"type": "keyword"},
                            "effects": _EFFECTS_MAPPING,
                            "effect": {"type": "text"},
                            "effect_value": {"type": "float"},
                            "effect_duration": {"type": "float"},
                        }
                    },
                }
            },
            # Ash of War affinities and the weapon classes it mounts on (#290).
            "default_affinity": {"type": "keyword"},
            "affinities": {"type": "keyword"},
            "affinity_whetblades": {
                "properties": {
                    "affinity": {"type": "keyword"},
                    "whetblade": {"type": "keyword"},
                }
            },
            "weapon_classes": {"type": "keyword"},
            "depicted_in_talisman": {"type": "keyword"},
            "depicts_weapon": {"type": "keyword"},
            # Talismans that can't be worn together (accessoryGroup, #295).
            "conflicts": {"type": "keyword"},
            # Armor damage negation (percent): raw, and net of the piece's resident
            # damage-taken effects (#284). Enemies: NpcParam cut rates x a humanoid's
            # armor loadout (#298).
            "negation": _NEGATION,
            "net_negation": _NEGATION,
            # Armor poise and the equipment menu's four resistances (#288).
            "poise": {"type": "float"},
            "status_resistance": {
                "properties": _props(
                    "integer", ("immunity", "robustness", "focus", "vitality")
                )
            },
            # Armor-alteration links (Boc / Master Hewg service).
            "alterable": {"type": "boolean"},
            "altered_variant": {"type": "keyword"},
            "altered_from": {"type": "keyword"},
            # Armor set membership (#325), from the EquipParamProtector row layout.
            "armor_set": {"type": "keyword"},
        },
    },
}

# Spirits that fight with a CharaInitParam loadout (the c0000 Puppets and DLC
# humans, #310) carry the humanoid enemy's gear groups, mapped the same way. One
# shared dict, so this covers summon_stats and max_level.summon_stats.
_SUMMON_STATS.update(
    {
        k: INDEX_MAPPING["mappings"]["properties"][k]
        for k in ("equipment", "weapon_attacks", "spell_attacks")
    }
)


# ---------------------------------------------------------------------------
# Mapping walkers
# ---------------------------------------------------------------------------


def _mapping_paths(props: dict, prefix: str = "") -> dict[str, dict]:
    """Every mapped field by dotted path ({path: mapping spec}), objects included."""
    out: dict[str, dict] = {}
    for fname, spec in props.items():
        path = prefix + fname
        out[path] = spec
        if "properties" in spec:
            out.update(_mapping_paths(spec["properties"], path + "."))
    return out


def _disabled_paths() -> frozenset[str]:
    """Mapped objects with enabled: false: kept in _source, never indexed (#446)."""
    return frozenset(
        path
        for path, spec in _mapping_paths(
            INDEX_MAPPING["mappings"]["properties"]
        ).items()
        if spec.get("enabled") is False
    )
