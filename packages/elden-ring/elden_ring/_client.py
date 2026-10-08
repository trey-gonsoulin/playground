"""OpenSearch client for self-hosted EC2 OpenSearch with basic auth + self-signed TLS."""

from __future__ import annotations

import fnmatch
import os
import time
import unicodedata
from typing import overload

import boto3
import requests
from opensearchpy import OpenSearch, RequestsHttpConnection

from elden_ring._calc import STATS, attack_rating, spell_damage

# ---------------------------------------------------------------------------
# Module-level cache — survives across warm Lambda invocations.
# Cold starts re-derive endpoint + password via one API call each.
# ---------------------------------------------------------------------------
_cached_endpoint: str | None = None
_cached_password: str | None = None
_cached_client: OpenSearch | None = None


def _ec2() -> "boto3.client":
    return boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _get_password() -> str:
    """Fetch the OpenSearch admin password from SSM SecureString (cached)."""
    global _cached_password
    if _cached_password:
        return _cached_password
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = ssm.get_parameter(
        Name=os.environ["OPENSEARCH_PASSWORD_SSM_PATH"],
        WithDecryption=True,
    )
    _cached_password = str(resp["Parameter"]["Value"])
    return _cached_password


def _get_current_endpoint() -> str:
    """Return the EC2 instance's current public DNS hostname.

    Raises RuntimeError if the instance is stopped (no public hostname assigned yet).
    """
    global _cached_endpoint
    if _cached_endpoint:
        return _cached_endpoint

    resp = _ec2().describe_instances(InstanceIds=[os.environ["EC2_INSTANCE_ID"]])
    instance = resp["Reservations"][0]["Instances"][0]
    hostname = instance.get("PublicDnsName") or instance.get("PublicIpAddress")
    if not hostname:
        raise RuntimeError(
            "Search service is offline. Call start_search_service() first."
        )
    _cached_endpoint = hostname
    return hostname


def get_client() -> OpenSearch:
    """Return a cached OpenSearch client, creating one if needed."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    host = _get_current_endpoint()
    _cached_client = OpenSearch(
        hosts=[{"host": host, "port": 9200}],
        http_auth=(os.environ["OPENSEARCH_USER"], _get_password()),
        use_ssl=True,
        verify_certs=False,  # demo installer uses a self-signed cert
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    return _cached_client


def start_instance(timeout_seconds: int = 240) -> str:
    """Start the EC2 instance, wait for OpenSearch to be healthy, return the hostname."""
    global _cached_endpoint, _cached_client

    instance_id = os.environ["EC2_INSTANCE_ID"]
    user = os.environ["OPENSEARCH_USER"]
    password = _get_password()

    ec2 = _ec2()
    ec2.start_instances(InstanceIds=[instance_id])

    # Wait for the instance to reach 'running' and get a public hostname.
    deadline = time.monotonic() + timeout_seconds
    hostname: str | None = None
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        dns = inst.get("PublicDnsName") or inst.get("PublicIpAddress")
        if inst["State"]["Name"] == "running" and dns:
            hostname = dns
            break
        time.sleep(5)

    if not hostname:
        raise TimeoutError("Instance did not reach running state in time.")

    # Invalidate stale cache now that we have a fresh hostname.
    _cached_endpoint = hostname
    _cached_client = None

    # Poll OpenSearch health endpoint until green/yellow or timeout.
    url = f"https://{hostname}:9200/_cluster/health"
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, auth=(user, password), verify=False, timeout=5)
            if r.status_code == 200 and r.json().get("status") in ("green", "yellow"):
                return hostname
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(10)

    raise TimeoutError("OpenSearch did not become healthy in time.")


# ---------------------------------------------------------------------------
# Index definition
# ---------------------------------------------------------------------------

# Overridable so a full reindex can target a new versioned index before cutover.
INDEX = os.environ.get("ELDEN_RING_INDEX", "elden-ring-entities")

_DAMAGE_TYPES = ("physical", "magic", "fire", "lightning", "holy")
_NEGATION_TYPES = (*_DAMAGE_TYPES, "strike", "slash", "pierce")
_STATS = ("str", "dex", "int", "fai", "arc")
_STATUSES = (
    "poison",
    "scarlet_rot",
    "bleed",
    "frostbite",
    "sleep",
    "madness",
    "death_blight",
)


def _props(type_: str, keys) -> dict:
    return {k: {"type": type_} for k in keys}


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
            for s in _STATS
        }
    },
    # On-hit status buildup (#118).
    "status_buildup": {"properties": _props("integer", _STATUSES)},
}

# Weapon poise damage per attack, by hand (#119); more attacks + powerstance (#122).
_POISE_ATTACKS = (
    "r1",
    "r2",
    "charged_r2",
    "guard_counter",
    "running_r1",
    "running_r2",
    "rolling_r1",
    "crouch_r1",
    "jumping_r1",
    "jumping_r2",
    "powerstance",
    # One-handed only (#126).
    "left_r1",
    "mounted_r1",
    "mounted_r2",
    "mounted_charged_r2",
    "mounted_jumping_r1",
    "mounted_jumping_r2",
    "mounted_left_r1",
    "mounted_left_r2",
    "mounted_left_charged_r2",
    "mounted_left_jumping_r1",
    "mounted_left_jumping_r2",
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
# the same stats for NG+1..NG+7 in one ng_plus group (#261).
_MIN_MAX = {"properties": _props("integer", ("min", "max"))}
_STAT_RANGES = {
    "stamina": _MIN_MAX,
    "defense": {"properties": {k: _MIN_MAX for k in _DAMAGE_TYPES[1:]}},
    "resistances": {"properties": {k: _MIN_MAX for k in _STATUSES}},
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

# Attack profile over an NpcParam row's move table (#81), shared by enemy docs and
# spirit-ash summon_stats (#124).
_NPC_ATTACKS = {
    "behavior_variation": {"type": "integer"},
    "count": {"type": "integer"},
    "damage_types": {"type": "keyword"},
    "elements": {"type": "keyword"},
    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
    # Max per-hit AtkParam_Npc poise damage (#253); spirits' x0.05 is folded in.
    "poise_damage": {"type": "float"},
    "status_buildup": {"properties": _props("integer", _STATUSES)},
    "status_effects": {"type": "keyword"},
}

# One spirit-ash summon entry (#86) at a given upgrade level (#117).
_SUMMON_STATS = {
    "count": {"type": "integer"},
    **_NPC_STATS,
    "damage_multiplier": {"type": "float"},
    # damage_multiplier x the resident vs-enemy corrections common to every element (#254)
    "damage_vs_enemies_multiplier": {"type": "float"},
    # Resident NpcParam slot SpEffects' damage cut rates (#181) x the vs-enemy damage
    # correction (#248); poise damage / status buildup received rates (#249)
    "damage_taken_multiplier": {"properties": _props("float", _NEGATION_TYPES)},
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
}
# kind any_of (#203): at least one of the nested (leaf) conditions holds
_QUEST_CONDITION = {**_QUEST_CONDITION, "conditions": {"properties": _QUEST_CONDITION}}
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
    ]
]

INDEX_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        # the nested set_when conditions (#231/#233) take the mapping past the
        # default 1000 fields; also applied to the live index with put_settings
        "mapping.total_fields.limit": 2000,
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
            "description": {"type": "text"},
            "text_content": {"type": "text"},
            # Item effect / info FMG lines (#90): WeaponEffect, AccessoryInfo,
            # GoodsInfo(2), ProtectorInfo, WeaponInfo.
            "effect_text": {"type": "text"},
            "info_text": {"type": "text"},
            "tags": {"type": "keyword"},
            "location": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
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
                    "follow_up_hits": {"type": "integer"},
                    "follow_up_attack_power": {
                        "properties": _props("integer", _DAMAGE_TYPES)
                    },
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
                    "motion_values": {"properties": _props("integer", _DAMAGE_TYPES)},
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
                    "summon_count": {"type": "integer"},
                    "summon_stats": {"properties": _SUMMON_STATS},
                }
            },
            "upgrade_curve": {"type": "object", "enabled": False},
            "ar_inputs": {"type": "object", "enabled": False},  # #120
            "requirements": {"properties": _props("integer", _STATS)},
            "fp_cost": {"type": "integer"},
            "spell_role": {"type": "keyword"},
            "slots": {"type": "integer"},
            "sort_id": {"type": "integer"},
            "menu_category": {"type": "keyword"},
            "npc_id": {"type": "keyword"},
            "name_source": {"type": "keyword"},
            "chr_models": {"type": "keyword"},
            # Enemy (NpcParam) combat stats, from the row bound by health bar / NameID
            # / spirit-ash label (#84).
            **_NPC_STATS,
            # Enemy attack profile over its model family's move table (#81).
            "attacks": {
                "properties": {
                    **_NPC_ATTACKS,
                    "shared_with": {"type": "keyword"},
                    # status_effects + every other_tables[].status_effects (#255).
                    "all_status_effects": {"type": "keyword"},
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
                    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
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
            "bosses": {"type": "keyword"},
            # Locations (map-menu regions + WorldMapPointParam markers, #77); name /
            # region / parent_region / map / positions / bosses shared with graces.
            "kind": {"type": "keyword"},
            "graces": {"type": "keyword"},
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
            # Warp / portal graph (EMEVD WarpToMap + cutscene warps, #94): each end
            # is filterable by map / grace / region / location; positions are
            # returned, not searched. warps_to / warps_from summarise it on
            # site_of_grace and location docs.
            "from": {"properties": _WARP_END},
            "to": {"properties": _WARP_END},
            "prompt": {"type": "keyword"},
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
                    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
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
                    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
                    "poise_damage": {"type": "float"},
                    "status_buildup": {"type": "object", "enabled": False},
                    # hits per cast and their summed power (#270), channelled
                    # spells, and the uncharged / charged casts (#269)
                    "hit_count": {"type": "integer"},
                    "attack_power_per_cast": {
                        "properties": _props("integer", _DAMAGE_TYPES)
                    },
                    "channeled": {"type": "boolean"},
                    **{
                        cast: {
                            "properties": {
                                "attack_power": {
                                    "properties": _props("integer", _DAMAGE_TYPES)
                                },
                                "poise_damage": {"type": "float"},
                                "hit_count": {"type": "integer"},
                                "attack_power_per_cast": {
                                    "properties": _props("integer", _DAMAGE_TYPES)
                                },
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
            "warp_region": {"type": "long"},
            "is_ending": {"type": "boolean"},
            "unskippable": {"type": "boolean"},
            "subtitles": {"type": "text"},
            "subtitles_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "talk_ids": {"type": "long"},
            "speakers": {"type": "keyword"},  # #191
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
            "flag_block": {"type": "long"},
            "related_npcs": {"type": "keyword"},
            "steps": {
                "properties": {
                    "phase_flag": {"type": "long"},
                    "order": {"type": "integer"},
                    "entered_from": {"type": "long"},
                    "locations": {"type": "keyword"},
                    "when": {"properties": _QUEST_CONDITION},
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
            "name_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "description_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "text_content_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "effect_text_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "info_text_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                    "lemma": {"type": "text", "analyzer": "kuromoji_lemmatizer"},
                },
            },
            "acquisition_types": {"type": "keyword"},
            "acquisition_sources": {"type": "keyword"},
            "dropped_by": {"type": "keyword"},
            # Per-killer drop scope (#230): placement vs type-wide, + chance.
            "drop_sources": {
                "properties": {
                    "enemy": {"type": "keyword"},
                    "scope": {"type": "keyword"},
                    "chance": {"type": "float"},
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
            "effects": {
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
            },
            "effect_duration": {"type": "float"},
            "infusable": {"type": "boolean"},
            "default_ash_of_war": {"type": "keyword"},
            "depicted_in_talisman": {"type": "keyword"},
            "depicts_weapon": {"type": "keyword"},
            # Armor damage negation (percent).
            "negation": {"properties": _props("float", _NEGATION_TYPES)},
            # Armor-alteration links (Boc / Master Hewg service).
            "alterable": {"type": "boolean"},
            "altered_variant": {"type": "keyword"},
            "altered_from": {"type": "keyword"},
        },
    },
}


def ensure_index(client: OpenSearch) -> None:
    if not client.indices.exists(index=INDEX):
        client.indices.create(index=INDEX, body=INDEX_MAPPING)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def analyze_text(client: OpenSearch, text: str) -> dict:
    """Return token streams for text under all three indexed JP analyzers.

    Calls OpenSearch's _analyze API via the field path so the result reflects
    exactly what each search mode applies:
    - standard: CJK unigram (description_ja), used by search_literal() default
    - kuromoji_segmenter: morpheme segmentation (description_ja.morph), use_kuromoji=True
    - kuromoji_lemmatizer: segmentation + baseform (description_ja.lemma), use_lemmatize=True

    Returns {"standard": [...], "kuromoji_segmenter": [...], "kuromoji_lemmatizer": [...]}
    """

    def _tokens(field: str) -> list[str]:
        resp = client.indices.analyze(index=INDEX, body={"field": field, "text": text})
        return [t["token"] for t in resp["tokens"]]

    return {
        "standard": _tokens("description_ja"),
        "kuromoji_segmenter": _tokens("description_ja.morph"),
        "kuromoji_lemmatizer": _tokens("description_ja.lemma"),
    }


def _availability_filter(include_unavailable: bool) -> list[dict]:
    """Filter clause excluding cut/unavailable content unless explicitly included.

    Unavailable content carries availability="cut" ([ERROR]-marked, scrapped) or
    availability="unobtainable" (real-named but with no acquisition path — enemy-only
    gear, reused assets; #71). Obtainable content has no availability field, so a
    must_not terms clause keeps unmarked docs and drops only the flagged ones.
    """
    if include_unavailable:
        return []
    return [
        {"bool": {"must_not": [{"terms": {"availability": ["cut", "unobtainable"]}}]}}
    ]


def _variant_filter(collapse_variants: bool) -> list[dict]:
    """Filter clause dropping item variant docs, keeping each family's base (#111).

    Weapon affinities (Heavy Dagger), talisman ranks (Erdtree's Favor +2), flask +N
    and altered armor are their own docs naming their base in ``base_item``;
    collapsing keeps the docs without it (bases, and everything outside a family).
    Supersedes the weapon-only collapse_affinity (#21).
    """
    if not collapse_variants:
        return []
    return [{"bool": {"must_not": [{"exists": {"field": "base_item"}}]}}]


# Source values from the retired erdb/fextralife layers (2026-09-21). An unknown
# filter value matches nothing, so name these specifically rather than returning 0.
_RETIRED_SOURCES = frozenset({"erdb", "fextralife"})


def _mapping_paths(props: dict, prefix: str = "") -> dict[str, dict]:
    """Every mapped field by dotted path ({path: mapping spec}), objects included."""
    out: dict[str, dict] = {}
    for fname, spec in props.items():
        path = prefix + fname
        out[path] = spec
        if "properties" in spec:
            out.update(_mapping_paths(spec["properties"], path + "."))
    return out


def _queryable_paths() -> frozenset[str]:
    """Mapped paths plus their subfields (description_ja.lemma, name.keyword)."""
    paths = set()
    for path, spec in _mapping_paths(INDEX_MAPPING["mappings"]["properties"]).items():
        paths.add(path)
        paths.update(f"{path}.{sub}" for sub in spec.get("fields", {}))
    return frozenset(paths)


def _unknown_fields(names: list[str] | None) -> list[str]:
    """Field names not in the mapping; a name with * passes if it matches any path."""
    if not names:
        return []
    paths = _queryable_paths()
    return [
        n
        for n in names
        if n not in paths
        and not ("*" in n and any(fnmatch.fnmatchcase(p, n) for p in paths))
    ]


def _precheck(source: str | None = None, *field_lists: list[str] | None) -> dict | None:
    """Error for a retired source or an unmapped field name, before any query (#205)."""
    if source in _RETIRED_SOURCES:
        return {
            "error": f"source '{source}' was retired 2026-09-21; all data is native — "
            "filter by entity_type, or call describe_fields() for the live sources"
        }
    unknown = [n for names in field_lists for n in _unknown_fields(names)]
    if unknown:
        return {
            "error": f"unknown field(s) {unknown}; call describe_fields() for the "
            "field catalog"
        }
    return None


def _check_filter_values(
    client: OpenSearch,
    entity_type: str | None = None,
    source: str | None = None,
    patch_version: str | None = None,
) -> dict | None:
    """Error naming a filter value no document carries, else None (#205).

    Called only after a query comes back empty: an unknown value always matches
    nothing, so a non-empty result needs no check.
    """
    if not (entity_type or source or patch_version):
        return None
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {
                "entity_type": {"terms": {"field": "entity_type", "size": 50}},
                "source": {"terms": {"field": "source", "size": 50}},
                "patch_version": {"terms": {"field": "patch_version", "size": 100}},
            },
        },
    )
    aggs = resp["aggregations"]

    def _keys(name: str) -> list[str]:
        return [b["key"] for b in aggs[name]["buckets"]]

    if entity_type and entity_type not in _keys("entity_type"):
        return {
            "error": f"unknown entity_type '{entity_type}'; "
            f"valid: {sorted(_keys('entity_type'))}"
        }
    if source and source not in _keys("source"):
        return {"error": f"unknown source '{source}'; valid: {sorted(_keys('source'))}"}
    if patch_version and patch_version not in _keys("patch_version"):
        return {
            "error": f"patch version '{patch_version}' is not loaded; "
            f"loaded versions: {sorted(_keys('patch_version'), key=_ver_key)}"
        }
    return None


def _field_coverage_warnings(
    client: OpenSearch, fields: list[str], entity_type: str | None
) -> list[str]:
    """A warning per named field that no document (of entity_type) carries (#205).

    Fields are checked on their base field, so description_ja.lemma counts
    description_ja. Each warning lists the text fields the type does carry.
    """
    bases = []
    for f in fields:
        base = f
        for suf in (*_JP_SUBFIELD_SUFFIXES, ".keyword", ".folded"):
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        bases.append(base)
    check = list(dict.fromkeys([*bases, *_LITERAL_FIELDS]))
    query = {"term": {"entity_type": entity_type}} if entity_type else {"match_all": {}}
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "query": query,
            "aggs": {
                "has": {
                    "filters": {"filters": {f: {"exists": {"field": f}} for f in check}}
                }
            },
        },
    )
    counts = {
        f: b["doc_count"] for f, b in resp["aggregations"]["has"]["buckets"].items()
    }
    carried = [f for f in _LITERAL_FIELDS if counts.get(f)]
    scope = f"{entity_type} documents" if entity_type else "documents"
    return [
        f"{f}: no {scope} carry this field; "
        f"text fields they carry: {', '.join(carried) or 'none'}"
        for f, base in zip(fields, bases)
        if not counts.get(base)
    ]


def search(
    client: OpenSearch,
    query: str,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    source: str | None = None,
    include_unavailable: bool = False,
    collapse_affinity: bool = False,
    collapse_variants: bool = False,
) -> list[dict] | dict:
    if err := _precheck(source, include_fields):
        return err
    patch_version = _canonical_version(client, patch_version)
    filters = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})
    filters += _availability_filter(include_unavailable)
    filters += _variant_filter(collapse_variants or collapse_affinity)

    body: dict = {
        "size": 0 if count_only else limit,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "name^3",
                                "display_name^2",
                                "name_ja^3",
                                "name_ja.ja^3",
                                "description",
                                "description_ja",
                                "description_ja.ja",
                                "text_content",
                                "text_content_ja",
                                "text_content_ja.ja",
                                "effect_text",
                                "effect_text_ja",
                                "effect_text_ja.ja",
                                "info_text",
                                "info_text_ja",
                                "info_text_ja.ja",
                                "location^1.5",
                                "tags^2",
                            ],
                            "type": "most_fields",
                            "fuzziness": "AUTO",
                        }
                    }
                ],
                "filter": filters,
            }
        },
    }

    if include_fields is not None:
        body["_source"] = include_fields

    if count_only:
        body["track_total_hits"] = True
        if not patch_version:
            # Cardinality agg gives distinct entity count; raw hits would be inflated
            # by multi-version docs.
            body["aggs"] = {
                "distinct_entities": {
                    "cardinality": {
                        "field": "name.keyword",
                        "precision_threshold": 40000,
                    }
                }
            }
    else:
        # Without a specific patch filter, collapse by entity name to deduplicate across
        # the 16+ indexed patch versions. Sort by _score first so groups stay in relevance
        # order (the primary contract of this tool), then patch_version desc as a tiebreak so
        # the representative doc per entity is the latest version. Without the tiebreak,
        # identical-content patches tie on _score and an arbitrary (often older) doc wins,
        # dropping fields stamped only on the newest patch (e.g. a talisman's effect).
        if not patch_version:
            body["sort"] = [{"_score": "desc"}, {"patch_version": "desc"}]
            body["collapse"] = {"field": "name.keyword"}

    resp = client.search(index=INDEX, body=body)

    if count_only:
        if not patch_version:
            out: list[dict] | dict = {
                "total": resp["aggregations"]["distinct_entities"]["value"]
            }
        else:
            out = {"total": resp["hits"]["total"]["value"]}
    else:
        out = [
            {"score": hit["_score"], **hit["_source"]} for hit in resp["hits"]["hits"]
        ]
    if not out or out == {"total": 0}:
        return _check_filter_values(client, entity_type, source, patch_version) or out
    return out


def _ascii_fold(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _newest_doc(client: OpenSearch, term: dict, entity_type: str | None) -> dict | None:
    filters: list[dict] = [{"term": term}]
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    resp = client.search(
        index=INDEX,
        body={
            "size": 1,
            "query": {"bool": {"filter": filters}},
            "sort": [{"patch_version": "desc"}],
        },
    )
    hits = resp["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def _resolve_entity_doc(
    client: OpenSearch, name: str, entity_type: str | None = None
) -> tuple[dict, bool] | None:
    """Newest doc matching an input name, plus whether the match was historical.

    Tries exact name, then diacritic-folded name (e.g. "Misericorde" finds
    "Miséricorde"), then display_name exact/folded for historical names of items
    renamed across patches (#59, #63). A display_name hit is the newest patch that
    still used the old name, so it's flagged historical.
    """
    folded = _ascii_fold(name)
    for term, historical in (
        ({"name.keyword": name}, False),
        ({"name.folded": folded}, False),
        ({"display_name.keyword": name}, True),
        ({"display_name.folded": folded}, True),
    ):
        doc = _newest_doc(client, term, entity_type)
        if doc:
            return doc, historical
    return None


def _resolve_entity_name(
    client: OpenSearch, name: str, entity_type: str | None = None
) -> str | None:
    """Canonical `name` for an input name (current, diacritic-folded, or historical)."""
    resolved = _resolve_entity_doc(client, name, entity_type)
    return resolved[0]["name"] if resolved else None


# get_entity returns placements only up to this many; common gathering materials
# have thousands (Rowa Fruit ~3,700, #135), far too much for one tool response.
PLACEMENTS_LIMIT = 50


def trim_placements(doc: dict | None, limit: int = PLACEMENTS_LIMIT) -> dict | None:
    """Drop a doc's ``placements`` list (and its variant_docs') when it is longer
    than ``limit``, keeping the count as ``placements_total``; maps, regions, areas
    and locations still summarize where it is."""
    if not doc:
        return doc
    pls = doc.get("placements")
    if pls is not None and len(pls) > limit:
        doc = {k: v for k, v in doc.items() if k != "placements"}
        doc["placements_total"] = len(pls)
    if doc.get("variant_docs"):
        doc = {
            **doc,
            "variant_docs": [trim_placements(v, limit) for v in doc["variant_docs"]],
        }
    return doc


def get_entity(
    client: OpenSearch,
    name: str,
    entity_type: str | None = None,
    include_variants: bool = False,
    patch_version: str | None = None,
) -> dict | None:
    requested_version = patch_version
    if patch_version:
        loaded = _entity_versions(client, None)
        patch_version = _canonical_version(client, patch_version)
        if patch_version not in loaded:
            return {
                "error": f"patch version '{patch_version}' is not loaded; "
                f"loaded versions: {loaded}"
            }
    resolved = _resolve_entity_doc(client, name, entity_type)
    if resolved is None:
        return _check_filter_values(client, entity_type)
    doc, historical = resolved
    if patch_version:
        # As-of the entity_type's own version set (#206): dialogue is loaded once
        # per Data0 group, so a patch between representatives maps to the earlier one.
        etype = doc["entity_type"]
        ev = _entity_versions(client, etype)
        resolved_version = _resolve_asof(patch_version, ev)
        if resolved_version is None:
            return {
                "error": f"no {etype} data at or before '{patch_version}' "
                f"(earliest loaded: {ev[0] if ev else 'none'})"
            }
        resp = client.search(
            index=INDEX,
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"name.keyword": doc["name"]}},
                            {"term": {"entity_type": etype}},
                            {"term": {"patch_version": resolved_version}},
                        ]
                    }
                },
            },
        )
        hits = resp["hits"]["hits"]
        if not hits:
            return {"error": f"'{doc['name']}' not present in {resolved_version}"}
        out = _public(hits[0]["_source"])
        if historical:
            out |= {"name_is_historical": True, "queried_name": name}
        if resolved_version != requested_version:
            out["requested_patch_version"] = requested_version
    elif historical:
        # A historical name matched an old patch; return the current doc instead of
        # stale stats, and say so (#63).
        newest = _newest_doc(client, {"name.keyword": doc["name"]}, doc["entity_type"])
        out = {
            **_public(newest or doc),
            "name_is_historical": True,
            "queried_name": name,
        }
    else:
        out = _public(doc)
    if include_variants:
        out["variant_docs"] = _family_docs(client, out)
    return out


# Larger than any item family (a base + 12 affinities).
_FAMILY_FETCH_SIZE = 50


def _family_docs(client: OpenSearch, doc: dict) -> list[dict]:
    """The rest of ``doc``'s item variant family at its patch, in sort_id order (#111).

    The family is the base (the doc's ``base_item``, or the doc itself) plus every doc
    naming that base in ``base_item``, same entity_type; ``doc`` itself is left out.
    """
    base = doc.get("base_item") or doc["name"]
    resp = client.search(
        index=INDEX,
        body={
            "size": _FAMILY_FETCH_SIZE,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"entity_type": doc["entity_type"]}},
                        {"term": {"patch_version": doc["patch_version"]}},
                    ],
                    "should": [
                        {"term": {"base_item": base}},
                        {"term": {"name.keyword": base}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "sort": [{"sort_id": {"order": "asc", "missing": "_first"}}],
        },
    )
    return [
        _public(h["_source"])
        for h in resp["hits"]["hits"]
        if h["_source"]["name"] != doc["name"]
    ]


# Calculator plumbing stored on docs but never returned or diffed (#120).
_INTERNAL_FIELDS: frozenset[str] = frozenset({"ar_inputs"})


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _INTERNAL_FIELDS}


def _ver_key(version: str) -> tuple:
    """Semantic sort key for a patch version ("1.10.1" > "1.9.0", not lexical)."""
    parts = []
    for p in str(version).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(-1)  # non-numeric (e.g. test versions) sort low, stable
    return tuple(parts)


def _entity_versions(client: OpenSearch, entity_type: str | None) -> list[str]:
    """Semver-sorted list of patch versions loaded for one entity_type (or all).

    Scoped to an entity_type because different types are loaded at different
    version sets — e.g. items exist at all 29 patches but npc_dialogue only at the
    18 Data0-group representatives. Pass None for the global set across all types.
    The agg size comfortably exceeds the loaded version count so no version is
    silently dropped as the timeline grows.
    """
    query = {"term": {"entity_type": entity_type}} if entity_type else {"match_all": {}}
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "query": query,
            "aggs": {"versions": {"terms": {"field": "patch_version", "size": 100}}},
        },
    )
    buckets = resp["aggregations"]["versions"]["buckets"]
    return sorted((b["key"] for b in buckets), key=_ver_key)


def list_patch_versions(client: OpenSearch) -> list[str]:
    return _entity_versions(client, None)


def _trimmed_key(version: str) -> tuple:
    """_ver_key without trailing zero parts, so "1.02.0" == "1.02", "1.10" == "1.10.0"."""
    key = list(_ver_key(version))
    while len(key) > 1 and key[-1] == 0:
        key.pop()
    return tuple(key)


@overload
def _canonical_version(client: OpenSearch, version: str) -> str: ...
@overload
def _canonical_version(client: OpenSearch, version: None) -> None: ...
@overload
def _canonical_version(client: OpenSearch, version: str | None) -> str | None: ...
def _canonical_version(client: OpenSearch, version: str | None) -> str | None:
    """The loaded patch label a requested version means, ignoring trailing zeros.

    Labels 1.02–1.06 have no third part while 1.07.0 on do, so "1.02.0" and
    "1.10" would otherwise be "not loaded". Returns the input unchanged when it's
    loaded already or doesn't match exactly one loaded label.
    """
    if not version:
        return version
    loaded = _entity_versions(client, None)
    if version in loaded:
        return version
    matches = [v for v in loaded if _trimmed_key(v) == _trimmed_key(version)]
    return matches[0] if len(matches) == 1 else version


def _resolve_asof(version: str, versions: list[str]) -> str | None:
    """Resolve a requested version to the latest loaded version <= it (as-of).

    Sparse entity types (dialogue is indexed once per Data0 group, not per patch)
    are still comparable at any patch pair: a requested version maps to the
    representative in effect at that point in the timeline. Returns None if the
    request predates everything loaded for the type.
    """
    if version in versions:  # exact snapshot — never surprise-resolve a real one
        return version
    key = _ver_key(version)
    candidates = [v for v in versions if _ver_key(v) <= key]
    return max(candidates, key=_ver_key) if candidates else None


def _flatten(doc: dict, prefix: str = "") -> dict:
    """Grouped stat objects as dotted leaves ({"stats": {"hp": 1}} -> {"stats.hp": 1}),
    so a diff names the stat that changed rather than the whole group (#115)."""
    out: dict = {}
    for k, v in doc.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[prefix + k] = v
    return out


_DIFF_SKIP_FIELDS: frozenset[str] = frozenset(
    {"entity_type", "patch_version", "source", "npc_id"}
)


def diff_entities(
    client: OpenSearch,
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
) -> dict:
    requested = {v1: _canonical_version(client, v1), v2: _canonical_version(client, v2)}
    v1, v2 = requested[v1], requested[v2]
    global_versions = set(_entity_versions(client, None))
    for v in (v1, v2):
        if v not in global_versions:
            return {
                "error": f"patch version '{v}' is not loaded; "
                f"loaded versions: {sorted(global_versions, key=_ver_key)}"
            }

    # Resolve as-of the (possibly sparser) version set for this entity_type.
    ev = (
        _entity_versions(client, entity_type)
        if entity_type
        else sorted(global_versions, key=_ver_key)
    )
    r1 = _resolve_asof(v1, ev)
    r2 = _resolve_asof(v2, ev)
    for orig, res in ((v1, r1), (v2, r2)):
        if res is None:
            return {
                "error": f"no data at or before '{orig}' for this entity type "
                f"(earliest loaded: {ev[0] if ev else 'none'})"
            }

    # Accept historical/diacritic-folded names; diff the canonical entity (#59).
    queried_name = name
    name = _resolve_entity_name(client, name, entity_type) or name

    def _fetch(version: str) -> dict | None:
        filters: list[dict] = [
            {"term": {"name.keyword": name}},
            {"term": {"patch_version": version}},
        ]
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        resp = client.search(
            index=INDEX,
            body={"size": 1, "query": {"bool": {"filter": filters}}},
        )
        hits = resp["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    doc1 = _fetch(r1)
    doc2 = _fetch(r2)

    if not doc1 or not doc2:
        # Distinguish "not in these patches" from "not in the index at all".
        # Report the resolved version so the message names where we actually looked.
        missing = [v for v, d in ((r1, doc1), (r2, doc2)) if not d]
        any_filters: list[dict] = [{"term": {"name.keyword": name}}]
        if entity_type:
            any_filters.append({"term": {"entity_type": entity_type}})
        exists_resp = client.search(
            index=INDEX,
            body={
                "size": 0,
                "query": {"bool": {"filter": any_filters}},
                "track_total_hits": True,
            },
        )
        if exists_resp["hits"]["total"]["value"] == 0:
            return {"error": f"'{queried_name}' not found in any loaded version"}
        missing_str = " and ".join(missing)
        return {"error": f"'{name}' not present in {missing_str}"}

    doc1, doc2 = _flatten(_public(doc1)), _flatten(_public(doc2))
    all_fields = (set(doc1) | set(doc2)) - _DIFF_SKIP_FIELDS
    changed: dict = {}
    unchanged: list[str] = []
    for field in sorted(all_fields):
        val1 = doc1.get(field)
        val2 = doc2.get(field)
        if val1 != val2:
            changed[field] = {v1: val1, v2: val2}
        elif val1 is not None:
            unchanged.append(field)

    result = {
        "name": name,
        "entity_type": (doc1 or doc2).get("entity_type"),
        "changed": bool(changed),
        "changed_fields": changed,
        "unchanged_fields": unchanged,
    }
    if queried_name != name:
        result["queried_name"] = queried_name
    if remapped := {k: v for k, v in requested.items() if k != v}:
        result["requested_patch_versions"] = remapped
    return result


# Per-pattern page size for the patterns-OR union path. Large enough that the
# deduped union count is exact (the corpus is a few thousand docs, well under the
# 10k max_result_window) rather than silently capped at the caller's `limit`.
_UNION_FETCH_SIZE = 10000

# Default literal-search fields: text only, by design. The acquisition keyword
# fields (sold_by/acquisition_sources/acquisition_types) are opt-in — a caller must
# name them via fields=[...] to search them (#61); an enum like acquisition_types in
# the default set would make a plain text search for "merchant" match every vendor item.
_LITERAL_FIELDS = [
    "name",
    "display_name",
    "description",
    "text_content",
    "effect_text",
    "info_text",
    "name_ja",
    "description_ja",
    "text_content_ja",
    "effect_text_ja",
    "info_text_ja",
]
# Same as above but Japanese fields routed through the segmentation-only .morph
# subfield so phrase queries respect kuromoji morpheme boundaries.
_LITERAL_FIELDS_MORPH = [
    "name",
    "display_name",
    "description",
    "text_content",
    "effect_text",
    "info_text",
    "name_ja.morph",
    "description_ja.morph",
    "text_content_ja.morph",
    "effect_text_ja.morph",
    "info_text_ja.morph",
]
# Same as above but Japanese fields routed through the lemmatizing .lemma
# subfield (kuromoji_baseform only) so a baseform query matches all inflections.
_LITERAL_FIELDS_LEMMA = [
    "name",
    "display_name",
    "description",
    "text_content",
    "effect_text",
    "info_text",
    "name_ja.lemma",
    "description_ja.lemma",
    "text_content_ja.lemma",
    "effect_text_ja.lemma",
    "info_text_ja.lemma",
]
# Japanese text fields whose analyzer lives on a subfield, not the base field.
_JP_BASE_FIELDS = {
    "name_ja",
    "description_ja",
    "text_content_ja",
    "effect_text_ja",
    "info_text_ja",
}
_JP_SUBFIELD_SUFFIXES = (".lemma", ".morph", ".ja")


def _route_literal_fields(
    fields: list[str], use_lemmatize: bool, use_kuromoji: bool
) -> list[str]:
    """Remap caller-supplied JP fields to the subfield carrying the active analyzer.

    The kuromoji analyzers live only on the .lemma/.morph subfields, so a plain
    fields=["description_ja"] would search the surface field and silently defeat
    use_lemmatize/use_kuromoji. English fields pass through untouched; a JP field
    given with or without an existing subfield suffix is normalized to its base and
    re-suffixed for the current mode, so both "description_ja" and "description_ja.morph"
    route correctly.
    """
    if use_lemmatize:
        suffix = ".lemma"
    elif use_kuromoji:
        suffix = ".morph"
    else:
        return fields
    routed: list[str] = []
    for f in fields:
        base = f
        for suf in _JP_SUBFIELD_SUFFIXES:
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        routed.append(base + suffix if base in _JP_BASE_FIELDS else f)
    return routed


def search_literal(
    client: OpenSearch,
    pattern: str | None = None,
    fields: list[str] | None = None,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 200,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    sort_id_gte: int | None = None,
    sort_id_lte: int | None = None,
    sort_id_mod: int | None = None,
    sort_id_remainder: int = 0,
    use_kuromoji: bool = False,
    patterns: list[str] | None = None,
    source: str | None = None,
    use_lemmatize: bool = False,
    include_unavailable: bool = False,
    collapse_affinity: bool = False,
    collapse_variants: bool = False,
) -> dict:
    """Exact-phrase search across text fields, with optional structural filters.

    When patch_version is None (default): collapses by entity name and returns the
    latest version per entity; total reflects distinct entities, not raw index hits.
    When patch_version is specified: filters to that snapshot; total is the raw hit count.

    use_kuromoji routes Japanese fields through .morph subfields (kuromoji_segmenter:
    tokenizer-only, no lemmatization/stopwords/stemming) so phrase queries respect
    dictionary word boundaries. Single-kanji queries like 象 will not match 象徴 or 象牙.

    use_lemmatize routes Japanese fields through .lemma subfields (kuromoji_lemmatizer:
    segmentation + kuromoji_baseform, no stopword/POS removal) so a single baseform
    query matches all inflected surface forms. Pass the dictionary form of the verb
    (e.g. 与える) to match 与えた, 与えられ, etc. Use analyze_text() to verify the
    expected baseform before querying. use_lemmatize takes precedence over use_kuromoji.

    An explicit fields list composes with both modes: Japanese fields named there
    (name_ja, description_ja, text_content_ja, effect_text_ja, info_text_ja) are routed
    to the matching .lemma/.morph
    subfield automatically, so fields=["description_ja"] with use_lemmatize=True searches
    description_ja.lemma. Without this, an explicit field would search the surface form and
    silently defeat the analyzer.

    Unknown values return {"error": ...} instead of a silent 0 (#205): a retired
    source or unmapped field name up front, and an entity_type / source /
    patch_version that no document carries when the result is empty. An empty
    result over a mapped field that no doc of the type carries adds "warnings".
    """
    if err := _precheck(source, fields, include_fields):
        return err
    requested_version = patch_version
    patch_version = _canonical_version(client, patch_version)
    out = _search_literal(
        client,
        pattern=pattern,
        fields=fields,
        entity_type=entity_type,
        patch_version=patch_version,
        limit=limit,
        include_fields=include_fields,
        count_only=count_only,
        sort_id_gte=sort_id_gte,
        sort_id_lte=sort_id_lte,
        sort_id_mod=sort_id_mod,
        sort_id_remainder=sort_id_remainder,
        use_kuromoji=use_kuromoji,
        patterns=patterns,
        source=source,
        use_lemmatize=use_lemmatize,
        include_unavailable=include_unavailable,
        collapse_affinity=collapse_affinity,
        collapse_variants=collapse_variants,
    )
    if not out["total"]:
        if err := _check_filter_values(client, entity_type, source, patch_version):
            return err
        if fields and (
            warnings := _field_coverage_warnings(client, fields, entity_type)
        ):
            out["warnings"] = warnings
    if patch_version != requested_version:
        out |= {
            "requested_patch_version": requested_version,
            "patch_version": patch_version,
        }
    return out


def _search_literal(
    client: OpenSearch,
    pattern: str | None = None,
    fields: list[str] | None = None,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 200,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    sort_id_gte: int | None = None,
    sort_id_lte: int | None = None,
    sort_id_mod: int | None = None,
    sort_id_remainder: int = 0,
    use_kuromoji: bool = False,
    patterns: list[str] | None = None,
    source: str | None = None,
    use_lemmatize: bool = False,
    include_unavailable: bool = False,
    collapse_affinity: bool = False,
    collapse_variants: bool = False,
) -> dict:
    if fields is not None:
        search_fields = _route_literal_fields(fields, use_lemmatize, use_kuromoji)
    elif use_lemmatize:
        search_fields = _LITERAL_FIELDS_LEMMA
    elif use_kuromoji:
        search_fields = _LITERAL_FIELDS_MORPH
    else:
        search_fields = _LITERAL_FIELDS
    filters: list[dict] = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})
    filters += _availability_filter(include_unavailable)
    filters += _variant_filter(collapse_variants or collapse_affinity)
    if sort_id_gte is not None or sort_id_lte is not None:
        sort_id_range: dict = {}
        if sort_id_gte is not None:
            sort_id_range["gte"] = sort_id_gte
        if sort_id_lte is not None:
            sort_id_range["lte"] = sort_id_lte
        filters.append({"range": {"sort_id": sort_id_range}})
    if sort_id_mod is not None:
        filters.append(
            {
                "script": {
                    "script": {
                        "source": "doc['sort_id'].size() > 0 && doc['sort_id'].value % params.mod == params.remainder",
                        "params": {"mod": sort_id_mod, "remainder": sort_id_remainder},
                    }
                }
            }
        )

    all_patterns: list[str] = []
    if pattern:
        all_patterns.append(pattern)
    if patterns:
        all_patterns.extend(patterns)

    if len(all_patterns) > 1 or patterns:
        # OpenSearch absorbs phrase clauses that share CJK tokens in a bool.should,
        # silently returning fewer results than any individual pattern alone (#64).
        # The only safe OR is one query per pattern with a Python-side union.
        #
        # Dedup keys on doc["name"], so name must always be in the sub-query source —
        # otherwise every doc collapses under "" and the union returns a single result.
        # Fetch a large per-pattern page so the deduped union (and thus total) is exact
        # rather than silently capped at `limit`; the returned list is trimmed to `limit`.
        want_name = include_fields is None or "name" in include_fields
        if count_only:
            sub_include: list[str] | None = ["name"]
        elif include_fields is None:
            sub_include = None
        elif want_name:
            sub_include = include_fields
        else:
            sub_include = [*include_fields, "name"]
        seen: dict[str, dict] = {}
        for p in all_patterns:
            r = _search_literal(
                client,
                pattern=p,
                fields=fields,
                entity_type=entity_type,
                patch_version=patch_version,
                limit=_UNION_FETCH_SIZE,
                include_fields=sub_include,
                count_only=False,
                sort_id_gte=sort_id_gte,
                sort_id_lte=sort_id_lte,
                sort_id_mod=sort_id_mod,
                sort_id_remainder=sort_id_remainder,
                use_kuromoji=use_kuromoji,
                patterns=None,
                source=source,
                use_lemmatize=use_lemmatize,
                include_unavailable=include_unavailable,
                collapse_affinity=collapse_affinity,
                collapse_variants=collapse_variants,
            )
            for doc in r.get("results", []):
                seen.setdefault(doc.get("name", ""), doc)
        if count_only:
            return {"total": len(seen)}
        results = list(seen.values())[:limit]
        if not want_name:
            results = [{k: v for k, v in d.items() if k != "name"} for d in results]
        return {"total": len(seen), "results": results}

    if not all_patterns:
        query_clause: dict = {"bool": {"must": [{"match_all": {}}], "filter": filters}}
    else:
        query_clause = {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": all_patterns[0],
                            "fields": search_fields,
                            "type": "phrase",
                        }
                    }
                ],
                "filter": filters,
            }
        }

    body: dict = {
        "size": 0 if count_only else limit,
        "track_total_hits": True,
        "query": query_clause,
    }

    if include_fields is not None:
        body["_source"] = include_fields

    if not patch_version:
        # Cardinality agg gives exact distinct-entity count; collapse + sort ensure the
        # representative doc per entity is the latest version.
        body["aggs"] = {
            "distinct_entities": {
                "cardinality": {"field": "name.keyword", "precision_threshold": 40000}
            }
        }
        if not count_only:
            body["sort"] = [{"patch_version": "desc"}, "_score"]
            body["collapse"] = {"field": "name.keyword"}

    resp = client.search(index=INDEX, body=body)

    if not patch_version:
        total = resp["aggregations"]["distinct_entities"]["value"]
    else:
        total = resp["hits"]["total"]["value"]

    if count_only:
        return {"total": total}
    return {"total": total, "results": [hit["_source"] for hit in resp["hits"]["hits"]]}


def _affinity_variant(client: OpenSearch, base: str, affinity: str) -> str | None:
    """Name of ``base``'s ``affinity`` variant when the affinity word isn't a prefix:
    the game names some variants mid-name ("Scavenger's Heavy Curved Sword")."""
    resp = client.search(
        index=INDEX,
        body={
            "size": 20,
            "_source": ["name"],
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"entity_type": "weapon"}},
                        {"term": {"affinity": affinity}},
                    ],
                    "must": [{"match": {"name": {"query": base, "operator": "and"}}}],
                }
            },
            "collapse": {"field": "name.keyword"},
        },
    )
    for hit in resp["hits"]["hits"]:
        name = hit["_source"]["name"]
        if name.replace(f"{affinity} ", "", 1) == base:
            return name
    return None


def calculate_attack_rating(
    client: OpenSearch,
    weapon: str,
    stats: dict,
    level: int | None = None,
    two_handed: bool = False,
    affinity: str | None = None,
    patch_version: str | None = None,
    spell: str | None = None,
) -> dict:
    """Attack rating / status buildup / spell scaling of one weapon for character
    ``stats`` at ``level`` (default max), from its doc's ar_inputs (#120). A name
    that isn't a weapon falls back to a thrown consumable (#178: Fire Pot, Kukri),
    whose ar_inputs have a single level scaled by its virtual weapon. With
    ``spell``, a staff or seal also returns that spell's attack power (#130)."""
    bad = {s: v for s, v in stats.items() if not 1 <= v <= 99}
    if bad:
        return {"error": f"stats must be 1-99: {bad}"}
    name = weapon
    if affinity and affinity != "Standard" and not weapon.startswith(f"{affinity} "):
        name = f"{affinity} {weapon}"
    entity_type = "weapon"
    canonical = _resolve_entity_name(client, name, "weapon")
    if canonical is None and not affinity:
        canonical = _resolve_entity_name(client, weapon, "consumable")
        if canonical is not None:
            entity_type = "consumable"
    if canonical is None and affinity:
        canonical = _affinity_variant(client, weapon, affinity)
    elif canonical is None and " " in weapon:  # "Heavy Scavenger's Curved Sword"
        canonical = _affinity_variant(client, *reversed(weapon.split(" ", 1)))
    if canonical is None:
        return {"error": f"weapon '{name}' not found"}
    versions = _entity_versions(client, entity_type)
    patch_version = _canonical_version(client, patch_version)
    version = _resolve_asof(patch_version, versions) if patch_version else versions[-1]
    if version is None:
        return {"error": f"no {entity_type} data at or before '{patch_version}'"}
    resp = client.search(
        index=INDEX,
        body={
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"name.keyword": canonical}},
                        {"term": {"entity_type": entity_type}},
                        {"term": {"patch_version": version}},
                    ]
                }
            },
        },
    )
    hits = resp["hits"]["hits"]
    if not hits:
        return {"error": f"'{canonical}' not present in {version}"}
    doc = hits[0]["_source"]
    inputs = doc.get("ar_inputs") or {}
    per_level = [
        arr
        for k in ("attack", "scaling", "status")
        for arr in (inputs.get(k) or {}).values()
    ]
    if not per_level and entity_type == "consumable":
        return {"error": f"'{canonical}' has no stat-scaled thrown attack in {version}"}
    if not per_level:
        return {
            "error": f"no attack-rating data for '{canonical}' in {version} (its "
            "element-correction row is missing from that patch's regulation)"
        }
    max_level = len(per_level[0]) - 1
    level = max_level if level is None else level
    if not 0 <= level <= max_level:
        return {"error": f"level must be 0-{max_level} for '{canonical}'"}
    full = {s: stats.get(s, 10) for s in STATS}
    notes = []
    if two_handed and entity_type == "consumable":
        # consumables are thrown/used, not wielded: no two-handing Str bonus
        two_handed = False
        notes.append("two_handed ignored: consumables are not wielded")
    result = {
        "weapon": canonical,
        "patch_version": version,
        "level": level,
        "max_level": max_level,
        "two_handed": two_handed,
        "stats": full,
        "requirements": doc.get("requirements"),
        **attack_rating(inputs, doc.get("requirements") or {}, full, level, two_handed),
    }
    if spell:
        cast = _spell_cast(client, spell, doc, result["spell_scaling"], version)
        if "error" in cast:
            return cast
        notes += cast.pop("notes", [])
        result.update(cast)
    if notes:
        result["notes"] = notes
    return result


# Catalyst menu_category -> the spell menu_category it casts (#130).
_CASTS = {"Glintstone Staff": "Sorcery", "Sacred Seal": "Incantation"}


def _spell_cast(
    client: OpenSearch, spell: str, catalyst: dict, scaling: dict | None, version: str
) -> dict:
    """A spell's attack power cast from ``catalyst`` (#130): the spell doc's own
    ``spell_attacks`` base (as of the catalyst's patch) x the catalyst's
    ``spell_scaling`` per damage type."""
    if scaling is None:
        return {
            "error": f"'{catalyst['name']}' is not a staff or seal: no spell scaling"
        }
    name = _resolve_entity_name(client, spell, "spell")
    if name is None:
        return {"error": f"spell '{spell}' not found"}
    spell_version = _resolve_asof(version, _entity_versions(client, "spell"))
    hits = (
        client.search(
            index=INDEX,
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"name.keyword": name}},
                            {"term": {"entity_type": "spell"}},
                            {"term": {"patch_version": spell_version}},
                        ]
                    }
                },
            },
        )["hits"]["hits"]
        if spell_version
        else []
    )
    if not hits:
        return {"error": f"spell '{name}' not present at or before {version}"}
    doc = hits[0]["_source"]
    entry = (doc.get("spell_attacks") or [{}])[0]
    base = entry.get("attack_power")
    if not base:
        return {
            "error": f"'{name}' deals no damage (a buff, heal or status-only spell)"
        }
    power = spell_damage(base, scaling)
    out = {
        "spell": name,
        "spell_attack_power": power,
        "spell_total": sum(v["total"] for v in power.values()),
    }

    # Hits per cast and their summed power (#270), the uncharged / charged casts
    # (#269). Scaling is linear per type, so the summed base scales as one.
    def per_cast(e: dict) -> dict:
        r = {}
        if e.get("hit_count"):
            r["hit_count"] = e["hit_count"]
        if e.get("attack_power_per_cast"):
            p = spell_damage(e["attack_power_per_cast"], scaling)
            r["total_per_cast"] = sum(v["total"] for v in p.values())
        return r

    cast_out = per_cast(entry)
    if "hit_count" in cast_out:
        out["spell_hit_count"] = cast_out["hit_count"]
    if "total_per_cast" in cast_out:
        out["spell_total_per_cast"] = cast_out["total_per_cast"]
    if entry.get("channeled"):
        out["spell_channeled"] = True
    for key in ("uncharged", "charged"):
        if (cast := entry.get(key)) and cast.get("attack_power"):
            p = spell_damage(cast["attack_power"], scaling)
            out[f"spell_{key}"] = {
                "attack_power": p,
                "total": sum(v["total"] for v in p.values()),
                **per_cast(cast),
            }
    casts = _CASTS.get(catalyst.get("menu_category"))
    if casts and doc.get("menu_category") and doc["menu_category"] != casts:
        out["notes"] = [
            f"a {catalyst['menu_category']} can't cast {doc['menu_category'].lower()}s"
        ]
    return out


def text_changed_between(
    client: OpenSearch,
    entity_type: str,
    field: str,
    v1: str,
    v2: str,
    count_only: bool = False,
) -> list[dict] | dict:
    """Return all entities of entity_type where field differs between v1 and v2.

    Fetches all entities for each version (up to 10 000 per call); comparison happens in Python.
    Only entities present in both versions are included (added/removed entities
    are excluded — use diff_entities for per-entity existence checks).
    """
    if err := _precheck(None, [field]):
        return err
    v1, v2 = _canonical_version(client, v1), _canonical_version(client, v2)
    # A requested version must be a real loaded patch somewhere in the index …
    global_versions = set(_entity_versions(client, None))
    for v in (v1, v2):
        if v not in global_versions:
            return {
                "error": f"patch version '{v}' is not loaded; "
                f"loaded versions: {sorted(global_versions, key=_ver_key)}"
            }

    # … but this entity_type may be indexed at a sparser set of versions (dialogue
    # is stored once per Data0 group), so resolve each request as-of that set.
    ev = _entity_versions(client, entity_type)
    if not ev:
        return {"error": f"no '{entity_type}' documents are loaded"}
    r1 = _resolve_asof(v1, ev)
    r2 = _resolve_asof(v2, ev)
    for orig, res in ((v1, r1), (v2, r2)):
        if res is None:
            return {
                "error": f"'{entity_type}' has no data at or before '{orig}' "
                f"(earliest loaded: {ev[0]})"
            }

    def _fetch_all(version: str) -> dict[str, object]:
        resp = client.search(
            index=INDEX,
            body={
                "size": 10000,
                "_source": ["name", field],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"entity_type": entity_type}},
                            {"term": {"patch_version": version}},
                        ]
                    }
                },
            },
        )
        return {
            hit["_source"]["name"]: _flatten(hit["_source"]).get(field)
            for hit in resp["hits"]["hits"]
        }

    docs_v1 = _fetch_all(r1)
    docs_v2 = _fetch_all(r2)

    results = []
    for name in sorted(set(docs_v1) & set(docs_v2)):
        val1 = docs_v1[name]
        val2 = docs_v2[name]
        if val1 != val2:
            results.append({"name": name, "text_before": val1, "text_after": val2})

    if count_only:
        return {"total": len(results)}
    return results


_ENTITY_COUNT_AGG = {
    "entity_count": {
        "cardinality": {"field": "name.keyword", "precision_threshold": 1000}
    }
}


def list_menu_categories(
    client: OpenSearch,
    entity_type: str | None = None,
    source: str | None = None,
) -> dict[str, int] | dict[str, dict[str, int]]:
    """Return distinct menu_category values with distinct entity counts.

    With entity_type: returns {category: entity_count} sorted by category name.
    Without entity_type: returns {entity_type: {category: entity_count}} for
    every type that has at least one doc with menu_category set.

    Counts are distinct-entity counts (cardinality on name.keyword), not raw
    doc counts, so multi-patch duplication doesn't inflate the numbers.

    An unknown or retired entity_type / source returns {"error": ...} (#205).
    """
    if err := _precheck(source):
        return err
    out = _menu_categories(client, entity_type, source)
    if not out:
        return _check_filter_values(client, entity_type, source) or out
    return out


def _menu_categories(
    client: OpenSearch,
    entity_type: str | None = None,
    source: str | None = None,
) -> dict[str, int] | dict[str, dict[str, int]]:
    if entity_type:
        filters: list[dict] = [{"term": {"entity_type": entity_type}}]
        if source:
            filters.append({"term": {"source": source}})
        body: dict = {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "categories": {
                    "terms": {"field": "menu_category", "size": 200},
                    "aggs": _ENTITY_COUNT_AGG,
                }
            },
        }
        resp = client.search(index=INDEX, body=body)
        return dict(
            sorted(
                (b["key"], b["entity_count"]["value"])
                for b in resp["aggregations"]["categories"]["buckets"]
            )
        )

    # Nested aggregation: entity_type → menu_category → distinct entity count
    base_filters: list[dict] = [{"exists": {"field": "menu_category"}}]
    if source:
        base_filters.append({"term": {"source": source}})
    body = {
        "size": 0,
        "query": {"bool": {"filter": base_filters}},
        "aggs": {
            "by_type": {
                "terms": {"field": "entity_type", "size": 50},
                "aggs": {
                    "categories": {
                        "terms": {"field": "menu_category", "size": 200},
                        "aggs": _ENTITY_COUNT_AGG,
                    }
                },
            }
        },
    }
    resp = client.search(index=INDEX, body=body)
    return {
        bucket["key"]: dict(
            sorted(
                (c["key"], c["entity_count"]["value"])
                for c in bucket["categories"]["buckets"]
            )
        )
        for bucket in resp["aggregations"]["by_type"]["buckets"]
    }


def list_entity_types(client: OpenSearch) -> list[str]:
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {"types": {"terms": {"field": "entity_type", "size": 50}}},
        },
    )
    return [b["key"] for b in resp["aggregations"]["types"]["buckets"]]


# Human-oriented notes for non-obvious queryable fields. Every mapped field is
# reported by describe_index with its type; these annotations add meaning for the
# ones a caller can't guess. Grouped stat objects are keyed by their dotted path
# (stats.hp); self-describing leaves (attack_power.fire, requirements.str) get no note.
_FIELD_NOTES: dict[str, str] = {
    "entity_type": "category filter, one of the entity_types listed above: equipment "
    "(weapon, armor, spell, item = talismans, ash_of_war, ammo), goods (consumable, "
    "key_item, info, crafting_material, upgrade_material, crystal_tear, spirit_ash, "
    "remembrance, great_rune, tool), merchant, enemy, boss (one doc per boss encounter, "
    "#79), site_of_grace, location, warp, quest, cutscene (one doc per realtime "
    "cutscene scene, #92), npc_dialogue, game_text (prompts, map banners, tutorials, "
    "loading tips, #98; item-use dialogs, #90). An unknown value returns an error listing these",
    "patch_version": "real game patch the doc was extracted from (native is per-patch); use with diff_entities",
    "source": "internal game-data origin — the param table, FMG or event script the doc "
    "was built from; the live values are listed under sources above (EquipParamWeapon, "
    "EquipParamGoods, Magic, NpcName, GameAreaParam, BonfireWarpParam, EMEVD, TalkMsg, …). "
    "Not 1:1 with entity_type (EquipParamGoods backs ten goods types; EMEVD backs warp, "
    "cutscene and quest). All data is first-party native extraction; the retired 'erdb' "
    "/ 'fextralife' values and any other unknown value return an error",
    "availability": "'cut' for content whose in-game name row is [ERROR]-marked (scrapped, "
    "e.g. Millicent's set); 'unobtainable' for real-named armor with no acquisition path — "
    "enemy-only gear / reused assets like the Ragged set (#71); absent for normal obtainable "
    "content. Both flagged states are excluded from search by default — pass "
    "include_unavailable=True to include them.",
    "display_name": "per-patch in-game FMG name; differs from name when an item was renamed across patches",
    "menu_category": "in-game equipment menu grouping (e.g. 'Straight Sword', 'Reaper', 'Head')",
    "sort_id": "in-game sort index; affinity variants are their base + N. Most "
    "armament bases are multiples of 1000, but not all: base-game Butchering Knife "
    "(2106500) and Prelate's Inferno Crozier (2504500), and many DLC armaments "
    "(+100 … +900 offsets), are not, so sort_id_mod silently drops them. Always use "
    "collapse_variants to count distinct armaments",
    "affinity": "infusable weapon's affinity (Standard, Heavy, Keen, … Occult); each affinity "
    "is its own doc, linked to the Standard row by base_item",
    "base_item": "on an item variant doc: its family's base. Weapon affinity -> Standard "
    "weapon ('Halberd' on 'Heavy Halberd'), talisman rank ('Erdtree's Favor' on "
    "'Erdtree's Favor +2'), flask +N ('Flask of Crimson Tears' on '... +6'), altered armor "
    "(as altered_from). Absent on bases and non-family items. Filter base_item=X to list "
    "a family; pass collapse_variants=True to search tools to keep only bases",
    "text_differs": "on a talisman rank variant: its text diverges from base_item beyond the "
    "effect-magnitude rewording every rank has (Boosts → Greatly boosts, 上昇 → 大きく上昇 are "
    "ignored). False = only magnitude wording changed",
    "text_added_lines": "the variant's text lines (EN + JP) with no counterpart in base_item, "
    "e.g. 「伝説のタリスマン」のひとつ on Erdtree's Favor +2",
    "tags": "free-form keyword tags (spell school/role, weapon category, 'Talisman', etc.); "
    "on game_text the kind: action_button, map_event, tutorial, loading_tip, item_dialog",
    "effect_text": "the game's own short effect lines for an item (list, #90): a weapon's or "
    "ammo's WeaponEffect lines ('Causes blood loss buildup', 'Boosts Crystalian sorcery'; "
    "the buildup number is in status_buildup), a talisman's AccessoryInfo ('Raises maximum "
    "HP'), a spell's GoodsInfo, a crystal tear's GoodsInfo2 ('Temporarily raises max HP'). "
    "In-game wording, unlike effect which is decoded from SpEffectParam and carries the "
    "numbers (Crystal Staff: 'Boosts Crystalian sorcery' here, '+10% attack (Crystalian "
    "sorceries)' in effect, #163)",
    "effect_text_ja": "Japanese effect_text; .ja/.morph/.lemma subfields drive JP search modes",
    "info_text": "the one-line info blurb from the item's Info FMG (#90): armor "
    "(ProtectorInfo, 'Helm worn by Kaiden sellswords'), ammo (WeaponInfo), a crafting "
    "material's gathering hint ('Found near churches and similar'), a spirit ash's "
    "summoned-spirit label (GoodsInfo2), and an Ash of War's in-game 'Usable on …' line "
    "(EquipParamGem.mountWepTextId → GR_MenuText 63xxx, menu line wraps joined; #169): "
    "'Usable on swords (colossal weapons excepted)'. The game's own summary of the "
    "classes; skill_poise_damage[].weapon_class lists them one by one from the "
    "canMountWep flags",
    "info_text_ja": "Japanese info_text; .ja/.morph/.lemma subfields drive JP search modes",
    "location": "where a merchant is found; on a boss doc, the legacy dungeon or area "
    "whose map holds the arena (PlaceName, e.g. Stormfoot Catacombs); absent for "
    "open-world bosses (see nearest_grace / region). Enemy and item placements name "
    "their dungeon location doc instead (see locations)",
    "sold_by": "merchant names that sell this item, derived per-patch from ShopLineupParam "
    "(includes Twin Maiden Husks for lineups they re-sell once given the bell bearing, #145). "
    "A row with no vendor label takes it from the NPC talk script that opens its range "
    "(#212): Moore's DLC stock, or the merchant whose other rows share the range",
    "shop_listings": "not searchable; returned by get_entity. One entry per ShopLineupParam "
    "row selling the item (#89): vendor, condition (the shop row's unlock label, e.g. a "
    "scroll/prayerbook, quest step or nomadic merchant site), price, currency (runes / "
    "Dragon Heart at Dragon Communion / Starlight Shards for Seluvis's puppets / Heart of "
    "Bayle at the Grand Altar; else the raw cost_type), quantity (stock; "
    "absent = unlimited), unlock_flag (the event flag that makes the row visible, e.g. a "
    "bell bearing handed to the Twin Maiden Husks; absent = always sold), requires_dlc (the "
    'paid DLC whose ownership flag gates the row: "Tarnished Pack" for its 1.17 stock), '
    "unlocked_by (the "
    "goods that set unlock_flag: handed over in the vendor's talk script — bell bearings, "
    "scrolls, prayerbooks, quest items like Seluvis's Potion — or picked up, #144), "
    "handed_to (the NPCs whose talk script takes that item: Seluvis's Potion given to "
    "Nepheli unlocks his Dolores puppet, given to the Dung Eater his Dung Eater puppet; "
    "Valkyrie's Prosthesis given to Millicent unlocks Gowry's Pest Threads), "
    "unlocked_by_defeating (bosses whose defeat sets it: Enia's remembrance-boss rows, "
    "Dragon Communion's dragons, #144), unlock_step + unlock_set_when for a flag with "
    "neither and no requires_dlc (#201): unlock_step is the quest step behind it, "
    "{quest, npc, phase_flag, order, life_state} as in npc_summons.requires_step (Black "
    "Flame's Protection at the Husks: Gideon phase 3968), unlock_set_when what turns "
    "it on when there's no step or a quest-only one, same shape as warp gate_set_when "
    "(Knight Bernahl's Ash of War: Eruption: a talk with Tanith); both absent when "
    "unresolved, resold_from + "
    "resale_flags on the Twin Maiden Husks' copies of another merchant's rows (#145: given "
    "a merchant's bell bearing, the Husks sell that lineup; unlocked_by / handed_to then "
    "cover both the bell bearing and the row's own unlock, e.g. Pest Threads resold from "
    "Gowry needs Gowry's Bell Bearing and Valkyrie's Prosthesis) and materials "
    "[{item, quantity}] for item costs (remembrance trades; some also charge runes, e.g. "
    "Grafted Dragon 2000 + Remembrance of the Grafted). E.g. Somber Smithing Stone [9]: "
    "Twin Maiden Husks, 25000 runes, unlimited, unlock_flag 11109759, unlocked_by "
    "[Somberstone Miner's Bell Bearing [5]]",
    "acquisition_types": "how the item is obtained, per-patch: merchant / enemy_drop / "
    "found_in_world / chest (a treasure-chest placement, #76) / corpse (looted from a "
    "body: a placement with on_corpse, #136; found_in_world still shows too) / "
    "given_by_npc (an NPC's talk "
    "script gives it, see given_by) / starting_equipment (a starting class's gear or item, "
    "see starting_classes) / keepsake (on the character-creation keepsake menu) (#23) / "
    "crafted (has a crafting recipe, see crafted_from, #87) / gathered (a gathering-node "
    "pickup such as a herb, butterfly or ore; placements with gathering, #142) / "
    "quest_reward (a map event script awards it once a quest flag is set, e.g. Rogier's "
    "Bell Bearing, the Volcano Manor rewards; #137. An award that also waits on a "
    "character's death, such as the Larval Tear of an enemy disguised as a Wandering "
    "Noble, is an enemy_drop instead; #265) / invader_drop (awarded for defeating "
    "an NPC invader, e.g. Hoslow's Petal Whip, Millicent's Prosthesis; #138) / altered (an "
    "(Altered) armor piece, made from its base piece, see altered_from, by the alteration "
    "service at a site of grace; not a sale, so no sold_by; #224) / interaction_reward "
    "(a map event script awards it when the player interacts with something, with no "
    "quest flag: a painting taken off its wall, a restored Great Rune at a "
    "Divine Tower, a DLC Ruined Forge furnace (its Ancient Dragon Smithing Stone and "
    "Anvil Hammer, Taylew the Golem Smith...), the Sanctified "
    "Whetblade, the Mirage Riddle, a Seed Talisman +1 for ringing a Finger Ruins bell "
    "(Hole-Laden Necklace held), the Stone-Sheathed Sword / "
    "Sword of Light / Sword of Darkness swaps; #147) / strike_reward (a map event "
    "script awards it for striking a character or object, e.g. the Golden Runes from "
    "hitting certain open-world characters; #266). found_in_world still shows for "
    "these, since their lots are map lots",
    "acquisition_sources": "named sources: merchant names, boss/named-enemy names (see "
    "dropped_by) and gift-giving NPC names (see given_by)",
    "given_by": "on an item doc: NPCs whose talk script gives the item (#23), named via the "
    "NPC's map placement. One script can serve several personas of the same character "
    "(Roderika / Roderika, Spirit Tuner) or a shared questline (Irina and Hyetta); gifts from "
    "scripts with no named placement (Melina, some DLC characters) have acquisition_types "
    "given_by_npc but no name. DLC gifts are indexed from 1.12.0 on",
    "in_exchange_for": "on an NPC-gift item doc: the item(s) the giving NPC takes for it, from "
    "the talk script's RemoveItem paired with the gift in the same dialogue state machine "
    "(#97), e.g. Volcano Manor Invitation: [Rya's Necklace]; Radiant Baldachin's Blessing: "
    "[Cursemark of Death]; Sellia's Secret: [Unalloyed Gold Needle]; Thiollier's Concoction: "
    "[Black Syrup]; Jolán and Anna <-> Swordhand of Night Jolán (a swap that keeps the "
    "upgrade level). Gurranq's Deathroot rewards are linked too (#193), with the count in "
    "in_exchange_count, e.g. Bestial Sling: [Deathroot]. Neutralizing Boluses (traded for an "
    "unnamed, param-less goods id) carries none. Name-keyed like given_by, so a same-named "
    "item (the DLC Beast Claw fist weapon) shows it too. The reverse of exchanged_for",
    "in_exchange_count": "on a counted turn-in reward (#193): which hand-in of the "
    "in_exchange_for item buys it, counting every one handed over so far (Gurranq takes all "
    "Deathroot held and keeps a running total), e.g. Clawmark Seal and Beast Eye: 1, Bestial "
    "Sling: 2, Ash of War: Beast's Roar: 4, Beast Claw: 5, Ancient Dragon Smithing Stone: 9. "
    "The fifth onward also need Gurranq's aggression event (in_exchange_flag 3647, #200). "
    "Absent on one-to-one turn-ins",
    "in_exchange_flag": "on a counted turn-in reward (#200): the event flag the talk "
    "script's branch for that hand-in also checks is on, beside in_exchange_count. Gurranq "
    "gives the fifth to ninth Deathroot rewards (Beast Claw, Stone of Gurranq, Beastclaw "
    "Greathammer, Gurranq's Beast Claw, Ancient Dragon Smithing Stone) only once his "
    "aggression event (flag 3647; he turns hostile after the fourth) has run. "
    "in_exchange_step names its quest "
    "step in the requires_step shape: {quest: 'Gurranq, Beast Clergyman', phase_flag: 3647, "
    "order: 3}. Absent when the hand-in needs no flag",
    "in_exchange_step": "see in_exchange_flag (#200)",
    "exchanged_for": "on a handed-in item doc: the NPC gift(s) handing it over gets (#97), the "
    "reverse of in_exchange_for, e.g. Rya's Necklace: [Volcano Manor Invitation]; a counted "
    "turn-in lists them in hand-in order (#193), e.g. Deathroot: [Clawmark Seal, Beast Eye, "
    "Bestial Sling, … Ancient Dragon Smithing Stone]",
    "duplication": "on an ash_of_war or remembrance doc: the menu that makes another copy of "
    "the item once you hold it (#223). Not a sale, so it never adds sold_by / shop_listings / "
    "an acquisition_types value; from the ShopLineupParam rows a talk script opens with a "
    "duplication opener",
    "duplication.service": '"Ash of War duplication" (Smithing Master Hewg\'s "Duplicate '
    'Ash of War" menu, every Ash of War) or "Remembrance duplication" (a Wandering '
    "Mausoleum, every patch; from 1.12 also the DLC Stone Coffin Altars, see also_at)",
    "duplication.where": '"Smithing Master Hewg" or "Wandering Mausoleum"',
    "duplication.also_at": "other places offering the same duplication from their own, "
    'separately stocked rows (#225): ["Stone Coffin Altar"] on a remembrance from 1.12 '
    "(the three DLC coffins, ShopLineupParam 102800-26); absent otherwise",
    "duplication.only_at_bell_mausoleums": "true on a remembrance the two bell-less "
    "Wandering Mausoleums in Liurnia don't offer (#227): their talk scripts open the "
    "duplication range 10 rows in, past the demigods' remembrances, so only the mausoleums "
    "with a boss bell duplicate them: the Grafted, Full Moon Queen, Starscourge, "
    "Blasphemous, Omen King, Blood Lord and Rot Goddess at every patch, plus the Impaler and "
    "A God and a Lord from 1.12; absent = every Wandering Mausoleum (and every also_at "
    "place)",
    "duplication.price": "the cost per copy: 1 (Lost Ashes of War) for an Ash of War; 0 for "
    "a remembrance (free)",
    "duplication.currency": '"Lost Ashes of War" (ShopLineupParam costType 4); absent when '
    "price is 0",
    "duplication.quantity": "copies per stock row: 1 for a remembrance (from 1.12 the "
    "also_at Stone Coffin Altars have their own stock row); absent = unlimited "
    "(Ashes of War)",
    "duplication.unlock_flag": "the event flag that makes the row visible: a per-ash flag "
    "(65810 + n, in EquipParamGem order) for an Ash of War, the boss-defeat flag for a "
    "remembrance (e.g. 9101 Remembrance of the Grafted)",
    "duplication.unlocked_by_defeating": "the boss(es) whose defeat sets a remembrance's "
    "unlock_flag, e.g. Remembrance of the Grafted: [Godrick the Grafted]",
    "starting_classes": "on an item doc: the starting classes whose initial loadout includes "
    "it (#23), e.g. Longsword: [Vagabond]; Memory of Grace: every class",
    "crafted_from": "on a craftable consumable/ammo doc: the crafting recipe's materials as "
    "[{item, quantity}] (ShopLineupParam_Recipe -> EquipMtrlSetParam, #87), e.g. Redmane "
    "Fire Pot: Mushroom x2, Smoldering Butterfly, Old Fang. Filter crafted_from.item to find "
    "what a material makes (or read used_in on the material)",
    "crafted_yield": "on a craftable doc: how many one craft makes (arrows/bolts 5 or 10)",
    "recipe_unlock": "on a craftable doc: the cookbook(s) whose pickup or purchase unlocks "
    "the recipe (#87); absent when the recipe is known from the start (Fire Pot, Rowa Raisin)",
    "used_in": "on a material doc: the items it is a crafting ingredient of (reverse of "
    "crafted_from, #87)",
    "unlocks_recipes": "on a cookbook doc: the items whose recipes it unlocks (#87)",
    "unlocks_shop_items": "on a goods doc (bell bearing, scroll, prayerbook, quest item) or "
    "boss doc: the items whose shop rows it unlocks — the reverse of shop_listings "
    "unlocked_by / unlocked_by_defeating (#144), e.g. Somberstone Miner's Bell Bearing [5]: "
    "[Somber Smithing Stone [9]]",
    "dropped_by": "enemies that drop this item: bosses/named enemies (map EMEVD + MSB "
    "placements, #68) and generic mobs named by their spirit-ash model label (#104). An "
    "enemy_drop item can have no dropped_by when the game data gives its killer no name "
    "(Teardrop Scarabs, the Fort Haight Godrick Knight dropping Ash of War: Bloody Slash; "
    "#153)",
    "drop_sources": "on item docs (#230): how each killer drops it, a list of "
    "{enemy, scope, chance}. scope 'placement' = a map-script award or death lot one "
    "placed instance gives (Commander O'Neil -> Commander's Standard); scope 'type' = "
    "the NpcParam death lot every enemy of that type rolls, with chance = its drop "
    "chance (0-1) when it's one simple roll (Godrick Soldier -> Smithing Stone [1], "
    "0.04; omitted when the item is in several slots, uses pity points, or the "
    "merged lots disagree). A unique character's own NpcParam lot is also 'type' "
    "(Blaidd the Half-Wolf -> Royal Greatsword, 1.0). One enemy can have both scopes. "
    "An entry with no enemy is a placement drop whose killer has no name in the data "
    "(Ash of War: Bloody Slash). Base chance only: Discovery (item discovery) raises it. "
    "Generic unnamed mobs' type drops have no entry; the names match dropped_by",
    "drops": "on an enemy doc: items this enemy drops (EMEVD awards + MSB death lots), "
    "merged over every encounter of the name; on a boss doc: the items awarded for that "
    "one encounter. Includes defeat rewards (remembrances, great runes) awarded when the "
    "boss's defeat flag turns on, and every row of a chained item lot (#134)",
    "equipment": "on a humanoid enemy/NPC/invader doc: the gear it is equipped with, from "
    "its map placement's CharaInitParam loadout (#85). Groups: weapons, ashes_of_war, armor, "
    "spells, talismans, ammo (item doc names; e.g. Recusant Henricus: Great Mace + Ash of "
    "War: Eruption). NPCs mostly use NPC-only copies of items; those are resolved to the real "
    "item they copy, and unnamed pieces (bare heads, placeholder armor) are left out. A name "
    "with several encounters lists the union. Absent on bosses/creatures without a loadout",
    "equipped_by": "on an item doc: enemies whose loadout includes it (reverse of "
    "equipment). A weapon's Standard doc carries it, not its affinity variants",
    "weapon_attacks": "on a humanoid enemy/NPC/invader doc: its attacks from the weapons "
    "in its loadout (#127), one entry per weapon + upgrade level + Ash of War (weapon, "
    "ash_of_war, reinforce_level, hands right/left). Read from the NPC's own weapon row, "
    "which can differ from the player's copy (Bloody Finger Nerijus's Reduvia +10 uses a "
    "regular upgrade path: physical 124, not the somber 193): attack_power and "
    "status_buildup are the weapon's numbers at that level, before stat scaling and "
    "the move's motion value, so they are NOT comparable with `attacks` (absolute "
    "per-hit AtkParam_Npc power, which humanoids usually lack); damage_types is the weapon's "
    "physical type, elements its non-physical damage. poise_damage = the largest "
    "first-hit poise over the weapon's attacks in stats.poise units (PvE, #119); "
    "poise_damage_by_attack holds each attack's first hit by hand (stored, not "
    "searchable). Bows/crossbows have no poise (the ammo decides)",
    "spell_attacks": "on a humanoid enemy/NPC/invader doc: its attacks from the spells "
    "in its loadout (#267), one entry per spell (spell = the item doc name; an NPC-only "
    "copy is named after the player spell it copies, e.g. Shabriri's two Unendurable "
    "Frenzy rows, fire 125 and 600; a hitting row no item name matches has magic_id "
    "instead, #268). On a spell doc: one entry, the spell's own hits (#130), the base "
    "calculate_attack_rating(spell=) scales by a catalyst. Read from the "
    "Magic row: its bullets (+ the bullets they spawn), for melee spells "
    "(Carian Slicer, Dragonmaw) its attack rows, and the hits its effects fire "
    "(Law of Causality's counter, holy 487; Carian Retaliation's parry glintblades), "
    "aggregated like `attacks`: "
    "attack_power = the largest per-hit flat power per element, i.e. the spell's base "
    "before catalyst scaling (Glintstone Pebble magic 152 = the wiki's 'Sorcery Scaling "
    "x 1.52'; a charged cast counts, so Lightning Spear 293 is its charged x 2.93); "
    "elements lists every element it deals, physical included (unlike "
    "weapon_attacks.elements); poise_damage = the largest per-hit flat poise in "
    "stats.poise units; status_effects the statuses it inflicts, status_buildup the "
    "largest per-hit buildup (stored, not searchable; Frenzied Burst madness 105). "
    "hit_count (#270) = every hit one cast lands on one target, and "
    "attack_power_per_cast those hits' flat power summed per element (Glintstone "
    "Stars 3 stars, magic 87 + 78 + 68; Stars of Ruin 12; Rock Sling 3 rocks; "
    "Carian Phalanx 9 glintblades x 48), taken from the cast with the larger sum "
    "(the charged one, when there is one). Every projectile, lingering hitbox and "
    "follow-on burst is assumed to connect and random-interval spawners spawn at "
    "their shortest interval, so both are upper bounds (Lightning Spear's 50-power "
    "burst, Elden Stars' lingering stars); which Magic slots one "
    "cast fires is inferred from the bullets' aim: slots aimed apart add up, "
    "same-aim or mirrored left/right slots are alternates (the strongest counts), "
    "and a swing (AtkParam ref) adds to the projectiles. Channelled "
    "hold-to-continue spells (Comet Azur, the dragon breaths, Meteorite of Astel) "
    "have channeled=true and no hit_count, as their ticks depend on how long the "
    "cast is held. A chargeable spell (Magic Staminacharge > 0) also has uncharged "
    "and charged = {attack_power, poise_damage, hit_count, attack_power_per_cast} "
    "per cast (#269; Lightning Spear lightning 234 / 293, the wiki's x2.34 / "
    "x2.93). The charged cast is the Magic row's bullets with id n >= 50 (1e7 + "
    "Magic id x 100 + n), its AtkParam refs with id % 10 >= 5 (DLC rows: % 100 >= "
    "50; Carian Piercer, the Crucible aspects) and its SpEffect refs whose hits are "
    "all charged rows; the split is kept only when the charged cast's strongest hit "
    "is stronger (equal power, as on Burn, O Flame! or Magic Downpour, or a weaker "
    "one, as on Roar of Rugalea, stays unsplit). attack_power stays the larger of "
    "the two. "
    "Buffs and heals (no damaging or status-inflicting hit, e.g. Golden Vow, Bloodflame "
    "Blade) are left out",
    "name_source": "on an enemy doc: where the name comes from — 'npc_name' (the per-character "
    "NpcName roster) or 'spirit_ash' (a generic-mob model label taken from its spirit ash, "
    "e.g. 'Godrick Soldier'; covers every placement of that model, #104)",
    "chr_models": "on a spirit_ash enemy doc: the chr model ids the label covers (e.g. c4311). "
    "Also on a boss/creature roster doc (name_source=npc_name) whose name a spirit ash shares, "
    "e.g. Crystalian: its field mobs' drops are merged into that doc (#106)",
    "effect": "readable effect text decoded from SpEffectParam, one phrase per effects "
    "entry, each timed one stating its own duration (e.g. Golden Vow: '+15% attack for "
    "80s; -10% damage taken for 80s'). On talismans, "
    "consumables, crystal tears, spells (#88), great runes (#156) and weapons, whose "
    "effect is the passive granted while held (#163: Blasphemous Blade '40 HP restored (on "
    "defeating an enemy); 4% max HP restored (on defeating an enemy)', Carian Regal "
    "Scepter '+10% attack (full moon sorceries)', Icon Shield '3 HP restored every 1s'; "
    "every affinity of a weapon shares it; the game's own wording is effect_text); a "
    "spell's or "
    "throwable's includes what its projectile inflicts on hit, worded by target (#160): "
    "'enemy loses 1 HP every 0.1s', '+100 frostbite buildup on enemy', 'cures poison on "
    "allies'; 'on self' is added only when the same stat also lands on someone else "
    "(Frenzied Burst: '+90 madness buildup on enemy; +20 madness buildup on self')",
    "effect_value": "primary numeric magnitude of the effect (the first effects entry's value)",
    "effects": "structured effects decoded from SpEffectParam (#88), one entry per stat: "
    "stat (e.g. 'attack' = every element, 'physical damage taken', 'damage taken while "
    "guarding' (a cut that applies only when blocking, #173: Ancient Dragon's Blessing's "
    "-20%, Shield Grease), 'HP restored', 'max HP restored', 'runes', 'all attributes', 'immunity' / 'robustness' / 'focus' = both "
    "resistances of that group, 'poison buildup', 'poison cured', or a value-less state "
    "such as Mohg's 'blessing of blood for summoned phantoms'), value (signed; % or points "
    "per unit, absent for cure / inflict / state), unit ('%' or 'points'), pvp_value (the value against "
    "players when it differs, e.g. Exalted Flesh 20 / 15), condition (the attacks or state "
    "it's limited to: 'charged attacks', 'jump attacks', 'skills', 'at full HP', 'HP at or "
    "below 20%', 'on hit', 'successive attacks', 'on defeating an enemy' (on-kill talismans "
    "such as Taker's Cameo), 'vs undead' (healing incantations' damage to undead enemies), "
    "'when blood loss / poison or rot / sleep / madness occurs within 7m' (#170: Exultation "
    "talismans, St. Trina's Smile, Poisoned / Madding Hand, Sacred Bloody Flesh; poison and "
    "rot share one trigger; the range is the game's status burst radius), 'until hit' "
    "(removed by the next hit: Opaline Bubbletear, Uplifting Aromatic), 'when hit by "
    "non-physical damage' (Crimsonwhorl Bubbletear's heal, #158), "
    "a spell school for catalyst boosts ('Glintblade sorceries', 'Dragon Cult "
    "incantations'), one ammo for bow boosts ('Radahn's Spear', 'Golden Arrow'), "
    "'requires Rune Arc'; stacking effects give one entry per tier, 'successive attacks, tier 1' … 'tier "
    "3', each value the tier's total, e.g. Winged Sword Insignia +3 / +5 / +10%; the bonus "
    "decays ~1.5s after the last hit), interval (seconds between regen / drain ticks, "
    "e.g. Bloodsucking Cracked Tear's 20 HP every 1s, #174), "
    "duration (seconds; absent = instant or while equipped; an on-hit proc of a timed "
    "buff takes the buff's duration, e.g. a grease's buildup lasts the grease's 60s), "
    "target (who it lands on, "
    "#160: 'self' = the user, 'enemy' = what the projectile or buffed weapon hits, e.g. "
    "Black Flame Blade's burn, grease procs, thrown-item buildup; 'ally' = allies only, "
    "e.g. Lord's Aid's second cure set; 'torrent' for raisins / horse effects), "
    "scales_with ('faith' / 'intelligence' for heals that scale). Stealth and stagger "
    "(#159): 'visibility to enemies' (how easily enemies spot you: Mimic's Veil -50%, "
    "Unseen Form -60%), 'sound heard by enemies' (Assassin's Approach -100%), and the "
    "value-less 'no stagger from minimal and small hits' (Baldachin's Blessing) / "
    "'... medium and large hits or pushback' (Leaden Hardtear, Ironjar Aromatic). Only "
    "confirmed fields are decoded, so some effects are missing (e.g. casting "
    "hyperarmor, which is animation data, not a SpEffect)",
    "effect_duration": "longest effects duration in seconds (absent = instant or permanent)",
    "is_legendary": "part of a legendary set (achievement-tracked)",
    "infusable": "weapon can take an affinity/ash-of-war infusion",
    "default_ash_of_war": "the skill a weapon ships with (from SwordArtsParam)",
    "depicts_weapon": "talisman depicts this weapon (lore cross-reference)",
    "depicted_in_talisman": "weapon depicted in this talisman (lore cross-reference)",
    "attack_power": "weapon/ammo attack power at +0 by damage type, as shown in game "
    "(affinity multiplier applied). Weapons also carry stamina (damage dealt to the "
    "target's stamina) and critical (critical-hit multiplier, 100 = base; daggers 130). "
    "Ammo carries every element it deals (Fire Arrow physical 15 + fire 95). Thrown "
    "consumables (#150: darts, knives, pots, stones) carry their hit's flat base power "
    "before stat scaling, e.g. Throwing Dagger physical 67, Fire Pot fire 230 (its "
    "burst). Most also carry stat scaling in scaling, which calculate_attack_rating "
    "applies (#178). Their attack rows' motion % is not applied (#182): Throwing "
    "Dagger reads physical 120 yet the wiki's damage tests match 67 x scaling. "
    "Freezing Pot, Roped Freezing Pot and the Hefty Freezing / Oil / "
    "Rot Pots have no hidden weapon row (VirtualWeaponID -1), so per the data they "
    "deal flat damage with no scaling",
    "projectile": "ammo standard-shot flight, from its Bullet param (#91; bow skills "
    "like Mighty Shot use other bullets, see skill_shots): speed / max_speed (m/s), range (metres "
    "flown before the shot starts to drop: Fletched bone arrows 30 vs 10), gravity "
    "(drop after range), lifetime (s), hit_radius (m). follow_up_hits counts the extra "
    "hits it spawns on impact (explosions, shockwaves, lightning strikes: Golem's Great "
    "Arrow, Explosive Bolt, Lightning Greatbolt), follow_up_attack_power their flat "
    "added power by element (Explosive Greatbolt fire 180) on top of the ammo's own. "
    "follow_up_motion_values lists each follow-up hit's motion value (#152): the % of "
    "the bow's attack rating it deals per element, identical hits merged with a count "
    "(Lightning Greatbolt: 1 strike at 100 then 5 at 30). Combining these with a bow's "
    "AR is calculator work, not precomputed. Thrown consumables (#150) carry it too: "
    "the thrown item's flight, follow_up_hits = the other hits one throw lands on a "
    "target (Fan Daggers' 4 extra blades, Lightning Pot's strike after the pot; an "
    "upper bound) with their follow_up_attack_power; no motion values (thrown items "
    "deal flat power)",
    "skill_shots": "ammo: the bow/crossbow skills' shots with this ammo (#151), one "
    "entry per skill and hit (FP and no-FP shots are separate hits, hit_label 'FP' / "
    "'no FP', or 'FP/no FP' when both fire the same hit; ammo with its own on-hit "
    "follow-up adds those hits as separate entries: Golem's Great Arrow, Lightningbone "
    "Arrow, Explosive / Lightning / Perfumer's Bolts), only for skills "
    "whose weapons fire this ammo type (Mighty Shot on arrows, Radahn's Rain on great "
    "arrows, Repeating Fire on bolts). motion_values = % of the bow's attack rating "
    "per element (Mighty Shot 153, Barrage 80 / no FP 40, Rain of Arrows 70; the "
    "standard shot is 100). poise_damage = the ammo's base poise x the hit's poise % "
    "(Arrow: Mighty Shot 6 vs 2 for a normal shot; Stormwing Bone Arrow 15; Rain of "
    "Arrows' no-FP hit really out-poises its FP hit, 100% vs 30%). "
    "hit_count = hits on one target per use, like "
    "skill_poise_damage.projectile_hit_counts (Repeating Fire 12, Fan Shot 8). Rain "
    "of Arrows 6 / Radahn's Rain 8 (#176): the volley's arrows share one hit list, so "
    "an arrow lands only after the last hit's 0.1 s record expires, every second "
    "arrow (Golem's arrows' 0.15 s record: every third, 4). For these volleys "
    "hit_count is an estimate, with the arrows dropping at their longest random "
    "interval (0.07 s; Radahn's Rain 0.09 s), and hit_count_max the upper bound when "
    "they drop at the shortest (0.06 s / 0.08 s): Rain of Arrows 7 (Golem's 5), "
    "Radahn's Rain 9. hit_count_max is present only when it differs from hit_count. "
    "On a small target some arrows miss, so fewer land. projectile = that hit's bullet flight, as in projectile (Mighty "
    "Shot speed 60 vs 40; Sky Shot range 99999 = no drop). Bows (their own skill) "
    "and bow Ashes of War carry the same entries for the standard ammo their weapon "
    "classes fire, named in skill_shots.ammo (#177): Ash of War: Barrage on Arrow; "
    "Ash of War: Rain of Arrows on Arrow and Great Arrow; Lion Greatbow's Radahn's "
    "Rain on Great Arrow; Repeating Crossbow's Repeating Fire on Bolt. Other ammo "
    "changes the numbers (Golem's Great Arrow's base poise 10 vs 7), so see the ammo "
    "docs for those. Crossbows and ballistae without a named skill have none. "
    "Skill shots come from the current-patch animations, so older patches list the "
    "skills with their own param values",
    "skill_shots.ammo": "bows and bow Ashes of War (#177): the standard ammo the "
    "entry's numbers are for (Arrow, Great Arrow, Bolt, Ballista Bolt); absent on "
    "ammo docs, whose entries are for the ammo itself",
    "status_effects": "ammo and thrown consumables (#150): statuses a shot or throw "
    "inflicts, from the item's and its bullets' on-hit SpEffects including "
    "damage-over-time chains (Rotbone Arrow: scarlet_rot, Kukri: bleed); the ammo "
    "buildup amount is in status_buildup, a consumable's in effects",
    "guard": "weapon block stats at +0, affinity multiplier applied: guarded damage "
    "negation % by type (physical/magic/fire/lightning/holy), boost (guard boost) and "
    "resistances (guarded status resistance by status)",
    "guard.boost": "guard boost: how well blocking withstands stamina damage",
    "guard.resistances": "guarded status buildup resistance: poison / scarlet_rot / bleed / "
    "frostbite / sleep / madness / death_blight (the in-game Guard 'Resist' line)",
    "scaling": "weapon attribute scaling at +0 by stat (str/dex/int/fai/arc), affinity "
    "multiplier applied; scaling.<stat>.grade is the in-game letter (S>=175 A>=140 B>=90 "
    "C>=60 D>=25 E>=1), scaling.<stat>.value the number it is graded from. "
    "Consumables whose effect is a stat-scaled bullet carry it too, from the hidden "
    "weapon row that bullet scales with (#178; the game shows no letters for them): "
    "thrown items like Kukri Str A / Dex S / Arc C and Fire Pot Str B / Dex D (Str A / "
    "Dex C at 1.02, when it dealt fire 122), and non-thrown ones like Lamenter's Mask, "
    "Innard Meat, Spritestone and Glinting Nail",
    "damage_types": "weapon/ammo physical damage type(s) as shown in game: Standard, "
    "Strike, Slash, Pierce (main type first, e.g. Halberd [Standard, Pierce]). Omitted on "
    "bows/crossbows/ballistas, whose damage type comes from the ammo",
    "status_buildup": "weapon/ammo on-hit status buildup at +0 by status (poison / "
    "scarlet_rot / bleed / frostbite / sleep / madness / death_blight), the in-game "
    "passive effect number, e.g. Uchigatana bleed 45. Cold/Poison/Blood affinities add "
    "or raise it and it grows with upgrade level (see max_level / upgrade_curve)",
    "poise_damage": "weapon poise (stance) damage per attack, first hit, by hand "
    "(one_handed / two_handed) and attack: r1, r2, charged_r2, guard_counter, "
    "running_r1, running_r2, rolling_r1, crouch_r1, jumping_r1, jumping_r2. One_handed "
    "only: powerstance (dual-wield L1 combo, e.g. Dagger 1.8 per hit) and "
    "powerstance_running/_rolling/_backstep/_jumping; left_r1 (left-hand light chain); "
    "mounted (Torrent) attacks mounted_r1, mounted_r2, mounted_charged_r2, "
    "mounted_jumping_r1/_r2 and the same swung to the left side as mounted_left_*. PvE "
    "values are in the same units as an enemy's stats.poise (Greatsword 2H charged R2 "
    "39.6; Dagger 1H jumping_r2 12); upgrades never change them. An attack a weapon "
    "lacks is absent (staves/seals/shields have no running/rolling/crouch rows). Ash of "
    "War / weapon skill hits are in skill_poise_damage. Full hit chains in "
    "poise_damage_chains. Omitted on bows/crossbows/ballistas (the shot's poise damage "
    "comes from the ammo)",
    "poise_damage.pvp": "the same attacks against players, on the wiki's displayed-poise "
    "scale (PvE x the attack's PvP rate x 10, e.g. Dagger 1H R1 40.5); compare with a "
    "player's displayed poise. Only from 1.07, when the PvP rates were introduced",
    "poise_damage_chains": "not searchable; returned by get_entity. Every hit of each "
    "chain, e.g. poise_damage_chains.pvp.one_handed.r1 = Dagger [40.5, 63, 63, 63, 63, "
    "126]. Jumping chains hold the jump's two hit rows: equal on most weapons, a "
    "stronger second hit on some two-handed jumps (Hookclaws two_handed.jumping_r1 "
    "[2.7, 5.4], Twinblade [3.25, 5])",
    "skill_poise_damage": "poise (stance) damage of the weapon's DEFAULT skill "
    "(default_ash_of_war; a unique weapon's fixed skill), from the judges the skill's "
    "animations fire: weapon base poise x each hit's rate, plus the hit's flat poise "
    "where it has one (spell-like hits: Carian Greatsword 20 / 35 charged, Ground Slam, "
    "roars 6), in the units of poise_damage "
    "and an enemy's stats.poise (Uchigatana Unsheathe max 30 = the heavy follow-up; "
    "Claymore Lion's Claw 33; Greatsword Stamp (Upward Cut) 36). Class and "
    "weapon-unique skill rows apply (Moonveil Transient Moonlight 22.5). On an "
    "ash_of_war doc: a list with one entry per weapon class the ash mounts on "
    "(weapon_class, weapon_poise, max, hits, pvp), e.g. Lion's Claw Straight Sword 30, "
    "Greatsword 33, Colossal Sword 36, Hammer 39, Great Hammer 42 / 45; class override "
    "rows apply (Wild Strikes light hits 100% on Curved Swords and Hammers vs 90%), "
    "weapon-unique rows don't. Projectile hits are listed separately in "
    "projectile_hits. Absent when the skill hits nothing (Quickstep, Parry, Endure) "
    "and on bow skills (the shot's poise comes from the ammo). Sort on "
    "skill_poise_damage.max for the heaviest-staggering skills",
    "skill_poise_damage.weapon_class": "ash_of_war docs only: the weapon class this "
    "entry is for (menu_category names, e.g. Katana, Colossal Weapon)",
    "skill_poise_damage.weapon_poise": "ash_of_war docs only: the class's weapon base "
    "poise the hits are computed from. A class whose ash-capable weapons have two bases "
    "gets two entries (Great Hammer 7 / 7.5, Claw 3 / 4, Colossal Weapon 6 / 7.5 at "
    "1.17)",
    "skill_poise_damage.max": "the skill's strongest single hit, melee or projectile "
    "(usually the FP'd heavy follow-up / charged version)",
    "skill_poise_damage.hits": "every distinct hit of the skill once, in animation order: "
    "FP and no-FP versions, light / heavy follow-ups and multi-hit parts (Uchigatana "
    "Unsheathe [15, 5, 30, 10] = FP light, no-FP light, FP heavy, no-FP heavy; see "
    "skill_poise_damage.hit_labels). Empty when the skill only fires projectiles",
    "skill_poise_damage.hit_labels": "one label per entry of hits (same order; pvp.hits "
    "too; projectile_hits have projectile_hit_labels): 'FP' (skill used with enough FP) "
    "or 'no FP' "
    "(the weaker version without), then the move when it isn't the skill's main swing: "
    "light / heavy (the R1 / R2 follow-up of a stance skill like Unsheathe, Square Off, "
    "Wild Strikes), follow-up / follow-up 2, early release / late release (a held skill "
    "let go), start / loop / loop end / end, roll, guard counter. 'FP/no FP' = the same "
    "hit either way. From the game's behavior graph states (the no-FP state of every "
    'move is its own animation). Filter e.g. hit_labels:"FP heavy"',
    "skill_poise_damage.projectile_hits": "the skill's projectile hits (waves, blades, "
    "flames, explosions and other spawned bullets), each distinct hit once in animation "
    "order, same units as hits. Weapon-scaled ones use the weapon's base poise (Moonveil "
    "Transient Moonlight waves [5, 7.5], Storm Assault's wind = the class base), "
    "spell-like ones a fixed value (Glintblade Phalanx 5 per blade on any weapon). "
    "Absent when the skill fires none. How often each lands is in "
    "projectile_hit_counts, which move fires it in projectile_hit_labels",
    "skill_poise_damage.projectile_hit_counts": "one count per entry of projectile_hits "
    "(same order): how many times that hit lands on one target in one use of the move, "
    "from the bullet params: bullets fired together, repeat shots and spawned bullets "
    "each count, but bullets sharing one hit list count once (Glintblade Phalanx 4 "
    "blades x 5 = the wiki's 5x4; Waves of Darkness 3 waves; Blasphemous Blade Taker's "
    "Flames 1 though it is 14 chained flames; Moonveil waves 1). Total projectile "
    "poise of a move = sum of projectile_hits x counts. An upper bound where bullets "
    "fan out (every bullet is assumed to reach the target; Flame Skewer's 5 flames). "
    "0 = a later segment of a wave that already hit (Sacred Relic Sword). Where FP and "
    "no-FP versions differ, the larger count",
    "skill_poise_damage.projectile_hit_labels": "one label per entry of "
    "projectile_hits (same order), like hit_labels: 'FP' / 'no FP' / 'FP/no FP' plus "
    "the move that fires it (Moonveil Transient Moonlight ['FP light', 'FP heavy'])",
    "skill_poise_damage.pvp": "the same hits against players on the wiki's displayed "
    "scale (PvE x the hit's PvP rate x 10); only from 1.07",
    "skill_poise_damage.pvp.projectile_hits": "projectile_hits against players, like "
    "skill_poise_damage.pvp",
    "reinforce_type_id": "weapon's ReinforceParamWeapon type (the upgrade path; affinity "
    "types are 100-offset), kept for traceability",
    "max_level": "weapon stats at its max upgrade, in the same shape as the +0 fields "
    "(attack_power / scaling / guard / status_buildup, affinity applied); max_level.level is the max "
    "(+25 regular, +10 somber, 0 if it can't be upgraded). Sort on "
    "max_level.attack_power.physical for the strongest fully upgraded weapons. On a "
    "spirit_ash doc: the summons at +10 (max_level.summon_stats / max_level.summon_count), "
    "e.g. filter max_level.summon_stats.stats.hp for the tankiest fully upgraded spirits",
    "upgrade_curve": "not searchable; returned by get_entity. The weapon's stats at every "
    "upgrade level as arrays indexed by level (upgrade_curve.attack_power.physical[25] = "
    "+25), only for stats that change with level; an absent stat keeps its +0 value. On a "
    "spirit_ash doc: upgrade_curve.summon_stats is a list aligned with summon_stats "
    "(upgrade_curve.summon_stats[0].stats.hp[10] = first spirit's +10 HP), plus "
    "upgrade_curve.summon_count when the number of spirits grows (Giant Rat Ashes 3 -> 5); "
    "a spirit's per-level attacks.attack_power is there too (#124). "
    "Weapons and spirit ashes also carry upgrade_curve.materials: the materials to reach "
    "each level as [{item, quantity}] (index 0 is null; Dagger materials[25] = Ancient "
    "Dragon Smithing Stone x1, Black Knife Tiche materials[1] = Ghost Glovewort [1]) (#87), "
    "and upgrade_curve.rune_cost: the runes to reach each level (index 0 is null; "
    "ReinforcePrice x the level's rate, somber's last step x6 after x5; Longsword "
    "rune_cost[25] = 1450, Moonveil rune_cost[10] = 2160, Black Knife Tiche "
    "rune_cost[10] = 14000; absent when the price is 0, e.g. Giant Rat Ashes) (#143)",
    "ar_inputs": "not searchable and not returned by get_entity: a weapon's per-level "
    "attack / scaling / buildup and correction curves, the inputs calculate_attack_rating "
    "uses to compute attack rating and Arcane status buildup for given stats; thrown "
    "consumables have one level (their flat power and buildup, #178)",
    "requirements": "attribute requirements by stat (weapons: str/dex/int/fai/arc; spells: "
    "int/fai)",
    "negation": "armor damage negation % by type: physical, strike, slash, pierce (physical "
    "sub-types), magic, fire, lightning, holy",
    "alterable": "armor piece can be altered (Boc / Master Hewg service)",
    "altered_variant": "name of the altered version of this armor",
    "altered_from": "name of the base armor this piece is altered from",
    "name_ja": "Japanese name; .ja/.morph/.lemma subfields drive JP search modes",
    "description_ja": "Japanese description; .ja/.morph/.lemma subfields drive JP search "
    "modes. On equipment and goods docs; npc_dialogue, game_text and merchant keep "
    "their JP text in text_content_ja",
    "description": "the item's in-game English description (flavor text); also the line "
    "itself on npc_dialogue / game_text and a stock summary on merchant. Absent on enemy, "
    "boss, location, site_of_grace, warp, quest and cutscene docs",
    "text_content": "searchable long text. Goods (consumable, key_item, …): the "
    "description; npc_dialogue / game_text: the line; merchant: the full stock list; "
    "cutscene: label + subtitles; enemy / boss / site_of_grace / location / warp / quest: "
    "the name plus its place names (a search aid, not game text). Absent on weapon, "
    "armor, spell, item, ash_of_war and ammo — search description there",
    "text_content_ja": "Japanese long text; .ja/.morph/.lemma subfields drive JP search "
    "modes. Present on goods types, npc_dialogue, game_text, merchant and cutscenes with "
    "subtitles only. Equipment (weapon, armor, spell, item, ash_of_war, ammo) has its "
    "JP text in description_ja instead, so naming text_content_ja there returns 0 plus a warning",
    "npc_id": "enemy's NpcName FMG id (6-digit humanoid / 9-digit boss & creature)",
    "stats": "enemy combat stats from one NpcParam row, bound by boss health bar, then "
    "NameID, then spirit-ash label (#84)",
    "stats.hp": "enemy base max HP (NpcParam, before per-area scaling; the in-game HP is "
    "stats_scaled.hp)",
    "stats.poise": "enemy max poise; absent when poise is disabled",
    "defense": "enemy elemental defense (NpcParam): magic, fire, lightning, holy. NpcParam has "
    "no physical defense. Base values, before per-area scaling (in-game: stats_scaled.defense)",
    "resistances": "enemy status buildup resistances (NpcParam): poison / scarlet_rot / bleed "
    "/ frostbite / sleep / madness / death_blight. 999 = immune; higher = more buildup needed. "
    "Base values, before per-area scaling (in-game: stats_scaled.resistances)",
    "immune_to": "enemy statuses at 999 resistance (immune), e.g. madness, death_blight",
    "traits": "enemy weakness classes from NpcParam flags: weak_to_gravity (bonus damage from "
    "gravity weapons), lives_in_death (Golden Order weapons), ancient_dragon, dragon "
    "(dragon-slaying weapons), undead. Empty list = none; absent = no NpcParam row bound",
    "weak_point_damage_multiplier": "enemy damage multiplier on hits to weak body parts",
    "attacks": "enemy attack profile (#81), aggregated over the move table of its NpcParam "
    "BehaviorVariationID (BehaviorParam -> AtkParam_Npc / Bullet -> on-hit SpEffect). A table "
    "belongs to a whole model family; the profile keeps only the moves this enemy's "
    "animations can fire (#123): unused rows and moves gated to another variant's "
    "SpEffect state are dropped (Commander Niall loses O'Neil's scarlet rot and keeps "
    "frostbite). Moves the AI alone chooses between stay, so a shared table can still "
    "carry a sibling's move (every soldier doc lists the Frenzied soldiers' madness). "
    "Moves have no names in the data, so there is no per-move list. The profile covers "
    "every MSB placement of the name on that table: placements that spawn with another "
    "gate state add their moves here and list them under attacks.state_variants (#180). "
    "Placements on another model's move table are not merged in; they are listed under "
    "attacks.other_tables (#250), whose statuses only attacks.all_status_effects folds "
    "in (#255). A name bound to a row with no move table takes its most-placed table "
    "(#256). Absent on humanoid NPCs/invaders, which fight with equipped weapons",
    "attacks.state_variants": "placed variants of the enemy that fire extra moves under "
    "a TAE gate SpEffect state the doc's bound NpcParam row lacks (#180), most placements "
    "first. Each entry has the profile (count / damage_types / elements / attack_power "
    "/ status_buildup / status_effects) of only the moves it adds. Gravebird: 46 "
    "placements under state 412 add the holy ring and poison tail, 6 under 413 add "
    "sleep. Where a gate state swaps the whole move set (Crystalian), an entry holds "
    "the whole alternate set, so attacks.count is a union no one placement fires "
    "(Crystalian 66 -> 131). Filter attacks.state_variants.status_effects for 'which "
    "enemies inflict X only in some placements'",
    "attacks.state_variants.special_states": "SpEffect SpecialState values these rows "
    "spawn with and the bound row doesn't (empty = same states, a different animation "
    "set)",
    "attacks.state_variants.placements": "MSB placements of the name on these rows",
    "attacks.state_variants.npc_param_ids": "the placed NpcParam rows of this variant",
    "attacks.other_tables": "placements of the same name on a different move table "
    "(BehaviorVariationID), one entry per table, most placements first (#250). Each entry "
    "has its behavior_variation, placements, npc_param_ids, the docs bound to that table "
    "(shared_with) and the profile of that table's moves. These moves are not in the "
    "top-level attacks, which stays the bound row's table; their statuses are in "
    "attacks.all_status_effects (#255). A boss's phase rows are left to phases",
    "attacks.other_tables.placements": "MSB placements of the name on this table",
    "attacks.other_tables.npc_param_ids": "the placed NpcParam rows on this table",
    "attacks.other_tables.shared_with": "enemy docs whose own attacks use this table",
    "attacks.attack_power": "max base attack power per element across the table (before "
    "per-area scaling; includes grabs and set-piece attacks)",
    "attacks.elements": "elements any move deals: physical / magic / fire / lightning / holy",
    "attacks.damage_types": "physical damage types across moves: Slash / Strike / Pierce / "
    "Standard",
    "attacks.poise_damage": "max per-hit poise (stance) damage across the table "
    "(AtkParam_Npc atkSuperArmor, #253), in the units of stats.poise (Black Knife "
    "Assassin 90, Erdtree Avatar 70, Tree Sentinel 60, Margit 35, Lone Wolf 10). On "
    "summon_stats.attacks it already includes every spirit's x0.05",
    "attacks.status_buildup": "max direct per-hit status buildup per status",
    "attacks.status_effects": "statuses any move inflicts, including damage-over-time "
    "effects with no per-hit value (Mohg's bloodflame bleed). Covers the bound table "
    "and attacks.state_variants, not attacks.other_tables",
    "attacks.all_status_effects": "statuses any of the enemy's placements inflict: "
    "attacks.status_effects plus every attacks.other_tables entry's (#255). Filter "
    "here for 'which enemies inflict X' (Demi-Human gains bleed from its second "
    "model's table)",
    "attacks.shared_with": "other enemies sharing this move table; non-empty means the "
    "profile may still include a move only their AI uses",
    "attacks.count": "distinct attack + projectile rows this enemy's moves reach",
    "attacks.behavior_variation": "NpcParam BehaviorVariationID (the move table id)",
    "grabs": "enemy grab attacks on the player (#82): the moves of the same move table as "
    "attacks whose catch hit starts a ThrowParam grab, plus the hits dealt during the "
    "throw, narrowed the same way (see attacks.shared_with). Absent = "
    "no grab. Filter exists:grabs for 'which enemies can grab you'",
    "grabs.count": "distinct grabs (ThrowParam rows for this enemy's model)",
    "grabs.attack_power": "max base attack power per element across grab hits (before "
    "per-area scaling)",
    "grabs.elements": "elements any grab hit deals: physical / magic / fire / lightning / "
    "holy",
    "grabs.damage_types": "physical damage types of grab hits: Slash / Strike / Pierce / "
    "Standard",
    "grabs.poise_damage": "max per-hit poise (stance) damage of grab hits (#253)",
    "grabs.status_buildup": "max direct per-hit status buildup of grab hits",
    "grabs.status_effects": "statuses a grab inflicts (Margit's grab: bleed)",
    "critical_hits": "which critical hits the player can land on this enemy (#128), from "
    "the player's ThrowParam rows for the bound NpcParam row's model; on a multi-phase "
    "boss, the union over its own phases (Rennala: stance_break from phase 2 only; see "
    "phases.critical_hits for which phase). All false = no critical at all (Tree "
    "Sentinel, Rykard). c0000 humanoids (NPCs, invaders) take the player-vs-player "
    "rows: backstab + riposte only (#241). Absent on unbound docs",
    "critical_hits.backstab": "a critical from behind is possible (false for Margit, "
    "Malenia, Crucible Knights)",
    "critical_hits.riposte": "a critical after a parry is possible; whether any of its "
    "attacks can be parried is a separate question (Guardian Golem has the row but is "
    "not parryable)",
    "critical_hits.stance_break": "a critical after a stance (posture) break is possible "
    "(the only critical of Radahn, Godrick, Mohg, Placidusax)",
    "critical_hits.downed": "a critical on the body on the ground, from any side, is "
    "possible (ThrowType 22, #240): riders knocked off their mount (Night's Cavalry, "
    "Godrick Knights, Kaiden Sellsword: the wiki's dismount critical); the same rows "
    "sit on seated families (merchants, Latenna, Nox)",
    "critical_hits.sleep": "a critical on the sleeping body, from any side, is possible "
    "(ThrowType 24, #240; Godskins, Vulgar Militia, Mad Pumpkin Heads, soldiers). A "
    "model-level row: whether this enemy can be put to sleep is a separate question "
    "(check immune_to; the dragons have the row but resist sleep)",
    "critical_hits.other_throw_types": "raw ThrowParam ThrowType values with no confirmed "
    "meaning (23 = a second rear arc with the backstab's reach, 10 = one row reached "
    "from above)",
    "team_type": "enemy NpcParam TeamType (#83): the team deciding whose attacks hit whom and "
    "who is targeted, so enemies with the same value are allies. Values without a team "
    "label are Elden Ring factions with no confirmed name (51 = demi-humans / imps / "
    "albinaurics / Rotten Stray, 9 = Runebear / trolls / Bols, 24 = Theodorix, 50 = "
    "Burial Watchdogs, 59 = Wormface, 60 = Misbegotten, 64 = Ghostflame Dragon)",
    "team": "readable team_type label where confirmed: enemy (6), boss (7, health-bar field "
    "bosses), arch_enemy (33, marquee bosses like Malenia and Radahn), friendly_npc (26), "
    "hostile_npc (27, invaders), none (0, untargetable NPCs/objects), cooperator (2, "
    "summonable-ally rows), spirit_summon (47), soldier (48, lord soldiers, Black Knights "
    "and Mad Pumpkin Heads), dragon (11), player (1, the host player's own team, joined "
    "by DLC NPC summons such as Ansbach)",
    "ai": "enemy AI perception profile (#83) from NpcThinkParam, the think row most used by "
    "the enemy's MSB placements; a tie goes to the row placed with the doc's stat row "
    "(the primary encounter: Belurat's Divine Beast Dancing Lion, not Rauh's), then "
    "the lowest id. Absent = never placed with a think row",
    "ai.think_id": "NpcThinkParam row id",
    "ai.sight_distance": "sight range in meters",
    "ai.sight_angle_width": "horizontal field of view in degrees",
    "ai.sight_angle_height": "vertical field of view in degrees",
    "ai.smell_distance": "smell (auto-detect, sees through walls) range in meters; absent = "
    "no smell",
    "ai.hearing_level": "hearing sensitivity (AI sound level it can hear; 128 is standard)",
    "ai.leash_distance": "how far (m) it chases from its home spot before returning; 999 / "
    "9999 = effectively unleashed (most bosses)",
    "ai.team_attack_weight": "0-100 pack-attack weight: higher lets fewer members of its team "
    "attack at the same time. Not friendly fire",
    "ai.guards": "raises its guard while acting (returning home, facing the target)",
    "stats_scaled": "enemy in-game stats over its MSB placements (#108, #131): hp, "
    "stamina, defense and resistances as min / max ranges, and ng_plus with the same "
    "stats for NG+1 .. NG+7. Each "
    "placement's NpcParam row is scaled by its SpEffect multipliers (the "
    "per-area scaling, plus e.g. x2 HP on field-boss versions of regular enemies). First "
    "playthrough, solo: multiplayer scaling is not applied. The top-level stats / "
    "defense / resistances stay the base values. A phase-2 boss sharing phase 1's "
    "character shows phase 1's values; see phases for per-phase values. Absent = never "
    "placed",
    "stats_scaled.hp": "enemy in-game max HP (#108) over its MSB placements: each "
    "placement's NpcParam row is base HP times its SpEffect HP multipliers, floored "
    "after each. NG+ is in stats_scaled.ng_plus.hp",
    "stats_scaled.hp.min": "lowest in-game HP over the enemy's placements",
    "stats_scaled.hp.max": "highest in-game HP over the enemy's placements (min = max "
    "when one encounter or all placements scale alike)",
    "stats_scaled.hp.placements": "number of MSB placements the range covers",
    "stats_scaled.stamina": "enemy in-game max stamina (#131) over its MSB placements: "
    "stats.stamina times the same SpEffect stamina multipliers as stats_scaled.hp, "
    "floored after each. min / max like stats_scaled.hp",
    "stats_scaled.defense": "enemy in-game elemental defenses (#131) over its MSB "
    "placements, per element (magic / fire / lightning / holy) a min / max: defense times the "
    "placement's area-scaling defense multiplier, floored (Malenia 100 -> 123, as the "
    "wiki). NpcParam has no physical defense field, so there is none here either",
    "stats_scaled.resistances": "enemy in-game status buildup resistances (#131) over "
    "its MSB "
    "placements, per status a min / max: resistances times the area-scaling resistance "
    "multiplier, rounded (Malenia bleed 154 -> 421, poison 542 -> 1481, as the wiki). "
    "Even the unscaled base-game area doubles them, so these are the values to compare "
    "with a weapon's status_buildup. 999 = immune, never scaled. Some wiki pages floor "
    "instead and read 1 lower (Radahn 334 / 243, ours 335 / 244). A multi-phase boss "
    "shows phase 1's row here: Messmer's frostbite is 435, while the wiki's 316 is his "
    "phase-2 row (base 112, see phases)",
    "stats_scaled.ng_plus": "enemy in-game stats on NG+1 .. NG+7 (#131, #261): hp, "
    "stamina, defense and resistances with the same keys as in stats_scaled, but each "
    "min / max is a 7-entry list, index 0 = NG+1 and 6 = NG+7 (later journeys play as "
    "NG+7). Each NG value is multiplied by the row's NG+ SpEffect (NpcParam "
    "NewGamePlusSpecialEffect; base-game rows 7400s, DLC 20007400s, on top of the area "
    "scaling), then by ClearCountCorrectParam's per-journey rate for that stat. The "
    "DLC-clear SpEffect (DlcGameClearSpEffectID) is not applied: when it applies is "
    "unknown",
    "stats_scaled.ng_plus.hp": "enemy in-game max HP on NG+1 .. NG+7 (#131; was "
    "hp_ng_plus before #261): NG+1 = the stats_scaled.hp value times the NG+ "
    "SpEffect's HP multiplier; NG+2..7 multiply that by ClearCountCorrectParam's "
    "per-journey HP rate (x1.1, 1.15, 1.2, 1.3, 1.35, 1.4), floored after each step. "
    "Matches the wiki's NG+ tables to within a few HP (they floor once): Malenia NG+7 "
    "26,250 per phase (wiki 47,249 for both), Messmer NG+ 41,093 (wiki 41,094)",
    "stats_scaled.ng_plus.hp.min": "lowest NG+1..NG+7 HP over the enemy's placements, "
    "one per journey",
    "stats_scaled.ng_plus.hp.max": "highest NG+1..NG+7 HP over the enemy's placements, "
    "one per journey",
    "stats_scaled.ng_plus.stamina": "enemy in-game max stamina on NG+1 .. NG+7 (#261), "
    "min / max 7-entry lists like stats_scaled.ng_plus.hp: the stats_scaled.stamina "
    "value times the NG+ SpEffect's stamina multiplier, then ClearCountCorrectParam's "
    "per-journey stamina rate (x1.1, 1.125, 1.2, 1.225, 1.25, 1.275), floored after "
    "each step",
    "stats_scaled.ng_plus.defense": "enemy in-game elemental defenses on NG+1 .. NG+7 "
    "(#261), per element a min / max pair of 7-entry lists (index 0 = NG+1): "
    "defense times the area and NG+ SpEffect defense multipliers, then "
    "ClearCountCorrectParam's per-journey defense rate (x1.025, 1.05, 1.1, 1.15, 1.2, "
    "1.3), floored once as stats_scaled.defense. Malenia 147 at NG+1 and 192 at NG+7, "
    "Messmer 126 / 164, as the wiki. The NG+ SpEffect stacks on the area one, as for "
    "HP; the wiki's NG+ defenses for Margit (117), Radahn (118) and Black Knife "
    "Assassin drop the area multiplier instead and read lower (ours 122 / 133)",
    "stats_scaled.ng_plus.resistances": "enemy in-game status buildup resistances on "
    "NG+1 .. NG+7 (#261), per status a min / max pair of 7-entry lists (index 0 = "
    "NG+1): resistances times the area and NG+ SpEffect resistance multipliers, then "
    "ClearCountCorrectParam's per-journey resistance rate (x1.015, 1.03, 1.045, 1.06, "
    "1.075, 1.09), rounded as stats_scaled.resistances. 999 = immune, never scaled. "
    "Malenia NG+7 bleed 459 and poison 1615, Messmer NG+ poison 474, as the wiki; "
    "Margit NG+ 345 (wiki 344, floored). The wiki's Radahn NG+ row (165) drops the "
    "area multiplier and reads below his NG 334 (ours 361)",
    "variants": "variants of one entity. Named variants are separate docs (Heavy Halberd, "
    "Erdtree's Favor +2), so on an item base this is a summary: one entry per doc naming it "
    "in base_item, in sort order, with name, affinity or rank, differs, and the values of "
    "the compact fields among attack_power / scaling / status_buildup (at +0) / weight / "
    "effect_value / is_legendary / availability, plus text_differs on talisman ranks; "
    "get_entity(include_variants=True) returns the full docs. Variants that share a "
    "display name can't be separate docs, so on an enemy each entry is a full stat block "
    "instead: enemy stat variants (#110): one entry per distinct NpcParam stat block "
    "over the name's boss-health-bar placements, e.g. Rennala's phase 2, the Scadutree "
    "Avatar forms, the Golden Shade Godfrey, the rot-immune Lake of Rot Dragonkin. The "
    "primary encounter (the doc's top-level stats) comes first. Rows that differ only in "
    "area scaling share an entry. Absent = one stat block (e.g. Night's Cavalry), or the "
    "enemy isn't bound by a health bar (humanoids, spirit-ash labels). Each entry has the "
    "enemy stat groups (stats / defense / resistances / immune_to / traits / "
    "weak_point_damage_multiplier) plus stats_scaled; a filter like "
    "variants.stats.hp matches "
    "if any variant matches",
    "variants.name": "item variant doc's name (get_entity it for the full doc)",
    "variants.affinity": "weapon variant's affinity (Heavy, Keen, … Occult)",
    "variants.rank": "talisman / flask variant's +N rank",
    "variants.differs": "top-level fields whose values differ from the base (stats, text, "
    "guard, max_level, poise_damage, …; names, sort order and acquisition are not "
    "compared). Only compact fields carry values in the entry",
    "variants.npc_ids": "NpcName ids whose health bar shows this stat block",
    "variants.npc_param_ids": "NpcParam rows in this stat block",
    "variants.stats_scaled": "in-game stat ranges over this block's placements (as "
    "the top-level stats_scaled: hp, stamina, defense, resistances and their NG+ "
    "lists in ng_plus)",
    "variants.maps": "MSB map ids of this block's placements (e.g. m12_01_00_00 for the "
    "Lake of Rot; DLC maps such as m61 are resolved too, #133)",
    "phases": "multi-phase boss fight (#132), the same list on every phase's doc: one "
    "entry per fighting character in phase order, from the map event scripts' boss "
    "events, e.g. Beast Clergyman -> Maliketh, Radagon -> Elden Beast, Godfrey -> "
    "Hoarah Loux, Malenia's two bars, Rennala's two phases, Fia's Champions -> Rogier -> "
    "Lionel. Each entry has the enemy stat groups of that character's placed NpcParam "
    "row (stats / defense / resistances / immune_to / traits / "
    "weak_point_damage_multiplier) plus its in-game values in stats_scaled (hp, "
    "stamina, defense, resistances and their NG+ lists in ng_plus) as single values. "
    "Total HP to beat the fight: sum "
    "stats_scaled.hp x (1 - ends_at_hp_ratio) over the entries, counting a shared HP pool once "
    "(skip entries with hp_pool_shared_with; the pool's own entry carries it). Absent = "
    "one-phase fight, a duo (co-bosses on one bar event), or a hand-off the scripts "
    "don't express as an HP check (Morgott)",
    "phases.phase": "1-based phase number; co-bosses in one phase share it (Lionel and "
    "two Fia's Champions)",
    "phases.name": "health-bar name of this phase's character",
    "phases.npc_id": "NpcName id shown on this phase's health bar",
    "phases.critical_hits": "critical_hits of this phase's NpcParam row alone (same "
    "leaves as the top-level field), e.g. Rennala's phase 1 has none and phase 2 the "
    "stance-break critical. Absent when the phase has no row",
    "phases.npc_param_id": "NpcParam row of this phase's fighting character",
    "phases.stats_scaled": "this character's in-game values (as the top-level "
    "stats_scaled, but single values for its one NpcParam row instead of ranges)",
    "phases.stats_scaled.hp": "this character's in-game max HP (area scaling applied, "
    "as stats_scaled.hp)",
    "phases.stats_scaled.stamina": "this character's in-game max stamina (as "
    "stats_scaled.stamina)",
    "phases.stats_scaled.defense": "this character's in-game defenses per element (as "
    "stats_scaled.defense)",
    "phases.stats_scaled.resistances": "this character's in-game status resistances "
    "(as stats_scaled.resistances)",
    "phases.stats_scaled.ng_plus": "this character's values on NG+1 .. NG+7 (as "
    "stats_scaled.ng_plus, but one 7-entry list per stat instead of min / max): hp, "
    "stamina, defense per element and resistances per status. Use ng_plus.hp in place "
    "of stats_scaled.hp for the NG+ fight total",
    "phases.ends_at_hp_ratio": "HP ratio at which the fight moves to the next phase: "
    "0.55 = at 55% HP left (Beast Clergyman), 0.0 = on death. Absent on the last phase or "
    "when the hand-off waits on something other than this character's HP",
    "phases.hp_pool_shared_with": "the phase whose HP pool this character's damage also "
    "drains, so both bars are one pool (Godfrey's damage feeds Hoarah Loux's 21,903)",
    "phases.heals_on_entry": "the phase starts with a scripted regeneration effect "
    "(Malenia, Goddess of Rot). How far it heals isn't in the params or event scripts "
    "(the wiki says 80%), so stats_scaled.hp is the full max HP",
    "enemies": "on a boss doc: the enemy doc names fought in this encounter, from its "
    "health bars (every phase and duo partner: Godfrey + Hoarah Loux, Radagon + Elden "
    "Beast, the two Night's Cavalry). The boss doc's name is the defeated character's "
    "(the last phase); a name shared by several encounters of the patch gets the place "
    "in parentheses: 'Night's Cavalry (Gate Town Bridge)', 'Erdtree Burial Watchdog "
    "(Stormfoot Catacombs)'",
    "boss_encounters": "on an enemy doc: the boss docs where it is fought (reverse of "
    "enemies)",
    "npc_summons": "on a boss doc: the NPCs whose summon sign the map script places for "
    "this fight, from the EMEVD sign templates joined on the boss's defeat flag. npc is "
    "the NPC's name (its enemy doc; a persona name where the game gives one: 'Castellan "
    "Jerren', 'D, Beholder of Death'), npc_id its NpcParam row, sign npc_white (the "
    "usual NPC summon sign), white (the DLC's plain white signs: Leda, Dane, Freyja at "
    "Rugalea) or festival (the Radahn festival's asset signs). requires_flag is the raw "
    "event flag the sign waits for (a quest step), absent when the sign is always "
    "there; an NPC listed twice has two alternative gates (Bernahl at the Godskin Duo). "
    "requires_step (#189) names the quest step behind that flag: {quest (the quest "
    "doc), npc (only when the quest name differs), phase_flag + order (the step in "
    "the quest doc's steps; Nepheli at Godrick: phase 4225, order 1)}, also through "
    "flags set by events gated on such a flag (hops, #228), or just the quest when "
    "only its manager event sets the flag or the only quest flags on in every event "
    "that sets it are that one quest's (Bernahl's life state at the Godskin Duo, "
    "#228). With no step, or a quest-only one, requires_set_when (#228) says what "
    "turns the flag on (see gate_set_when): Jerren at Radahn = a SpEffect on Radahn "
    "(kind character, state special_effect, entity 1052380800) while Starscourge "
    "Radahn is not yet defeated, with the player in their own world (in_own_world); "
    "Freyja at the Dancing Lion = Moore's phase 4927 not yet reached",
    "summonable_for": "on an enemy doc: the boss docs where this NPC can be summoned "
    "(reverse of npc_summons)",
    "hostile_signs": "on an enemy doc: the NPC's signs tied to no boss fight. kind "
    "invasion is a sign the game places by itself when the player walks into an area, "
    "i.e. an NPC invasion (Bloody Finger Nerijus, Vyke, Moore after a Pest is killed); "
    "duel is a red sign the player touches (the Knight of the Great Jar). map is where "
    "it happens, sign_type the raw PlaceSummonSign type (21/22/23 for invasions, not "
    "decoded), requires_flag the raw event flag it waits for, absent when ungated, "
    "and requires_step its quest step when a quest phase sets it (same shape as "
    "npc_summons; #189), and requires_set_when when there's no step or a quest-only "
    "one: what turns the flag on (as gate_set_when; #228). These sign invasions are separate from the Ceremony-instance "
    "invasions of placements' world_state / a location's invasion_instances (#96): "
    "no NPC has both on one map",
    "cutscene_id": "on a cutscene doc (#92): the scene's cutscene id (AABBNNNN; the doc "
    "name is 'Cutscene <id>'), the one its map scripts play. Scene ids differing only in "
    "the last digit (a …0 / …1 pair, Melina's eight 60420000-60420007 meetings) are "
    "one doc; the others are in variant_ids. On a warp doc of kind cutscene (#94): the "
    "cutscene that plays before the warp (PlayCutsceneToPlayerAndWarp). On an "
    "npc_dialogue doc (#192): the scene the line is a subtitle of (see cutscene)",
    "cutscene": "on an npc_dialogue doc (#192): the cutscene doc ('Cutscene <id>') the "
    "line is a subtitle of, the reverse of that doc's talk_ids. Absent on lines no "
    "cutscene speaks; a line several scenes share (the 3 opening narration lines "
    "of the ending scenes) names the lowest id. On a warp doc with a cutscene_id "
    "(#189): the cutscene doc that plays (variant ids folded)",
    "variant_ids": "on a cutscene doc: the other cutscene ids folded into this scene",
    "asset": "on a cutscene doc: the cutscenebnd asset name (s10_00_0010 for "
    "10000010); absent when the current game files hold no such asset (15000020)",
    "label": "on a cutscene doc: a hand-written scene name for 22 scenes whose "
    "subtitles, warp or item gate identify them (#191: 'Melina's first meeting', "
    "'Age of the Stars ending', 'Grand Lift of Dectus', 'Opening cinematic'), else a "
    "native label: the boss and trigger kind ('Margit, the Fell Omen: boss intro'), else the quest of a quest "
    "scene ('Dung Eater: quest', #189), else the kind and first "
    "subtitle line ('scripted: Greetings.'), else the kind and map",
    "trigger_kind": "on a cutscene doc: how its map scripts trigger it. boss_intro (in "
    "a boss's 28xx fight-event block, before the fight), boss_defeat (waits on / sets "
    "a boss's defeat flag, and doesn't also play with it off: the Siofra Aqueduct "
    "coffin waits on the Valiant Gargoyles' defeat, but the Grand Cloister coffin to "
    "Astel plays either way so is scripted; #271), ending (cutscene flag 64 or an ending-choice flag "
    "9400-9409; keeps the Elden Beast boss link), item (gated on holding an item: the "
    "medallions of the Dectus / Rold / Haligtree lifts, but also e.g. any Flask of "
    "Crimson Tears or Messmer's Kindling), quest (a trigger flag is an NPC quest step, "
    "see trigger_steps; #189), scripted (anything else: area arrivals, world-state "
    "flags)",
    "boss": "on a cutscene doc: the boss doc the scene introduces or follows (see "
    "trigger_kind); the boss doc lists it back under cutscenes",
    "trigger_flags": "on a cutscene doc: the event flags its script waits to be on. "
    "Not listed (#271, #274): a flag the scene also plays with off (Jerren's "
    "festival-started flag 9411; a boss flag, see trigger_kind), a flag that only "
    "picks one alternative of a wait (the endings' 'Frenzied Flame 108 not taken, "
    "or cured 116'), or one the script exits on (Gostoc's gate scene waits on "
    "10009374 or 10009377 but ends on 10009377)",
    "trigger_steps": "on a cutscene doc (#189): the quest steps behind its "
    "trigger_flags, one per distinct step, each {quest, npc, phase_flag, order, "
    "life_state} as in npc_summons.requires_step (Patches' 60370000: phase 3688). "
    "Any trigger kind can carry it; a scripted scene with one is trigger_kind quest",
    "trigger_set_when": "on a cutscene doc (#228): for each trigger_flag with no "
    "quest step (unlinked, or linked to a quest only), {flag, when}: what turns it on, as warp gate_set_when (Jerren's "
    "festival scene 60510000: flag 9410 = any_of(talk to Iji / Sellen, Roderika's "
    "phase 3063) while no other festival flag 9411-9413 is on, with the player in "
    "their own world; flag 9411 = talk to Castellan Jerren with 9410 already on; the Frenzied "
    "Flame endings' flag 108 = the player wearing no armor (armor_equipped Head, "
    "Body, Arms, Legs: the empty slots) and pressing 'Open door' on entity "
    "35001500, in their own world; flag 1051362702 = talk to Castellan Jerren, "
    "#232). A flag is left out when it links to a phase or life_state step, or when "
    "its set_when would be absent for any of the reasons gate_set_when lists",
    "trigger_items": "on a cutscene doc: the items the player must hold (Dectus "
    "Medallion (Left) / (Right) for the Grand Lift of Dectus)",
    "warp_region": "on a cutscene doc: the MSB region entity the player is moved to "
    "after the scene (…AndWarp plays)",
    "is_ending": "on a cutscene doc: an ending cutscene (cutscene flag 64 or an "
    "ending-choice flag)",
    "unskippable": "on a cutscene doc: the cutscene flags forbid skipping (flag 2)",
    "subtitles": "on a cutscene doc: the spoken lines in timeline order (EN), from the "
    "scene's timeline (current-patch MQB) joined through TalkParam to the patch's "
    "TalkMsg text; who speaks them is in speakers. Every line is also an npc_dialogue doc "
    "(see talk_ids). About half the scenes have none (no dialogue)",
    "subtitles_ja": "on a cutscene doc: the Japanese lines matching subtitles",
    "talk_ids": "on a cutscene doc: the TalkMsg ids of its subtitles, in order; the "
    "npc_dialogue doc for a line is named 'Dialogue <id>'",
    "speakers": "on a cutscene doc (#191): who speaks its subtitles, in order of their "
    "first line (Margit's intro: ['Margit, the Fell Omen']; the opening cinematic: "
    "['Narrator']). Hand-mapped from the TalkMsg id bank (talk_id // 10000: 2003 = "
    "Margit, 2049 = Ranni the Witch), since TalkParam names no speaker; a bank with "
    "two voices names its main one. Absent with no subtitles or an unmapped bank",
    "cutscenes": "on a boss doc (#92): the cutscene docs linked to the encounter, each "
    "{id: cutscene_id, kind: boss_intro / boss_defeat / ending}",
    "region": "on a site_of_grace doc: the grace's map-menu region (Stormhill, Liurnia "
    "of the Lakes, Leyndell, Ashen Capital, Gravesite Plain); on a boss doc: the "
    "region of the grace closest to the arena, even where nearest_grace skips that "
    "post-fight grace (a teleport-only arena takes its nearest_grace's); on a location "
    "marker doc: the region of its nearest grace. Every region value is also "
    "a location doc. Enemy and item docs carry it per placement; filter them on regions",
    "parent_region": "on a site_of_grace doc: the map-menu tab its region sits under "
    "(Stormhill and Weeping Peninsula -> Limgrave; Castle Ensis -> Gravesite Plain); on "
    "a location doc: the tab above its region (a subregion's own tab)",
    "kind": "on a location doc (#77): region (a map-menu tab: Limgrave, Caelid, "
    "Stormveil Castle, Gravesite Plain; also the world-map area labels such as Realm of "
    "Shadow), subregion (a tab's sub-category: Stormhill, Weeping Peninsula, Leyndell, "
    "Ashen Capital), or a marker kind from its world-map icon: catacombs, cave, tunnel, "
    "ruins, church, shack, rise, evergaol, hero_grave, fort, castle, settlement, "
    "legacy_dungeon, divine_tower, tower, minor_erdtree, miquellas_cross, gaol, forge, "
    "well, mausoleum, colosseum, gate, grand_lift. Absent on markers whose icon has no "
    "label (unique landmarks). A marker named like a region merges into the region doc. "
    "On a warp doc (#94): waygate (a portal: 'Travel to another location?'), "
    "return_to_entrance (a dungeon boss room's shortcut back), evergaol (entering an "
    "evergaol, or the exit after its boss), cutscene (a warp that plays a cutscene: "
    "coffins, Astel's drop, Maliketh -> Ashen Capital) or scripted (other script "
    "warps: trap chests, quest steps, the Radahn festival)",
    "graces": "on a location doc: the site_of_grace docs in it (a region: graces in the "
    "region or its subregions; a dungeon: graces in its map)",
    "area_scaling": "on a location doc (#77): the enemy area-scaling tiers of the "
    "enemies placed there, most placements first. Each: speffect_id (the NpcParam "
    "SpecialEffectID3 SpEffect: 7000s base game, 20007000s DLC), placements (placed "
    "enemies on that tier), and the multipliers hp, stamina, attack (all damage types), "
    "defense (all types) and resistance (all statuses except death blight, which is "
    "never scaled). A region counts placements by their nearest grace's region (a tab "
    "sums its subregions); a dungeon counts its map's placements. The first tier is "
    "the area's typical level (Caelid 7070: hp x2.406). Humanoid NPC tiers "
    "(19351-19370) are left out. An open-world landmark (ruins, fort, church) counts "
    "the enemies inside its MSB footprint (#80); one without a footprint has none",
    "footprint": "on an open-world landmark location doc (#80): the MSB MapPoint "
    "volumes that outline it, the same shapes placements are matched against. Each: "
    "{shape (box | cylinder | sphere), map, world_position (the base centre, same frame "
    "as placements), rotation_y (degrees), width / depth / height | radius (metres)}; "
    "a composite landmark lists every part. Returned, not searchable",
    "invasion_instances": "on a location doc (#96): the NPC-invasion versions of the "
    "map there (the game's Ceremony instances: a Volcano Manor request, a Bloody "
    "Finger or quest invasion). Each: {ceremony (the instance id, 20-50), hosts (the "
    "enemy docs that exist only in that instance, the host first), invader_flag (the "
    "event flag set when the invader is beaten)}. Sellen / Jerren and Millicent's "
    "help / betray choices are two instances of one map",
    "nearest_grace": "on a boss doc: the site_of_grace doc closest to the arena (world "
    "coordinates in the open world, same map otherwise; a grace more than 30 m above "
    "or below counts as farther, so a cliff's foot or another floor loses); locates "
    "open-world bosses. It's the grace before the fight: a grace that only appears "
    "once a boss is beaten (Godrick the Grafted; also another boss's, such as "
    "Crucible Knight's Redmane Castle Plaza for Starscourge Radahn) is skipped while "
    "another grace qualifies, and the arena's own unless it's the map's only one, so "
    "Godrick gives Secluded Cell. An open-world arena next to a legacy dungeon also "
    "weighs that dungeon's graces: Scadutree Avatar -> Tree-Worship Sanctum. "
    "Fight order and fog walls aren't in the data, so a grace just past the arena "
    "can still win (Sir Gideon Ofnir). A teleport-only arena "
    "with no grace of its own (Hallowhorn Grounds, Chapel of Anticipation) gives the "
    "grace at the warp's other end: Ancestor Spirit -> Siofra River Bank. The boss "
    "doc's region, and the place in parentheses when a shared boss name takes a grace "
    "('Crucible Knight (Redmane Castle Plaza)'), are still the arena's own grace's",
    "map": "MSB map id: a boss doc's arena, a site_of_grace doc's map (a dungeon grace "
    "gives the dungeon's map even though its world-map marker is on the overworld), a "
    "location marker's map (a dungeon's own map, likewise), a cutscene doc's map (where "
    "the play warps the player, else mAA_BB_00_00 from the cutscene id). "
    "m10_00_00_00 Stormveil; open world m60_XX_YY_00 tiles, DLC m61",
    "position": "on a site_of_grace or location doc outside the open world: its "
    "position in map's local coordinates. Also the key used inside placements entries",
    "world_position": "on a site_of_grace or location doc in the open world (and "
    "dungeon graces and markers, which sit on the overworld): world coordinates, tile "
    "x 256 + local, so "
    "distances compare across tiles. The base map (m60) and the DLC map (m61) are "
    "separate frames. placements entries on open-world tiles use the same frame, so "
    "they compare with grace and boss positions",
    "placements": "on enemy and item docs (#76): where it is in the world, from the "
    "map MSBs. Enemy: one entry per placed instance of the name (health-bar, NameID or "
    "spirit-ash label), {map, world_position | position, entity_id}. Item: one entry "
    "per pickup (MSB treasure event: corpse, chest, or an enemy carrying it), {map, "
    "world_position | position, lot_id, in_chest, on_corpse}; in_chest is true only "
    "for treasure chests (altar and tree pickups aren't chests); on_corpse is true when "
    "the pickup is a body (its MSB part is a corpse model) or lies on a corpse asset "
    "within 0.75 m (#136); absent doesn't rule out a body (a few wiki bodies have no "
    "corpse asset in the map data); plus one entry per "
    "gathering node (an herb, flower, butterfly, mushroom or ore asset whose model "
    "carries the pickup lot, #135), {map, world_position | position, lot_id, "
    "gathering: true}, so common materials have thousands; enemy drops are not "
    "pickups (see dropped_by). Open-world tiles (m60 base, m61 DLC, any tile size) give "
    "world_position; dungeons and legacy maps give map-local position. A part copied "
    "on an open-world variant tile (mAA_XX_ZZ_1S, a world-state copy of the _0S tile) "
    "is listed once, on the _0S tile (#184); only parts that exist on the variant "
    "alone keep the variant map id, and which world state a variant is isn't in the "
    "data. Each entry also "
    "carries region / parent_region (its nearest grace's, #140) and, in a dungeon map, "
    "location (the dungeon's location doc). On open-world tiles, area is the game's "
    "own region map at that spot (#80: the map-name texture, or a map-name volume "
    "that renames the spot on entry), which can differ from the nearest grace's region "
    "at borders (Stormhill vs Limgrave) and in stacked DLC areas (Rauh Base vs Scadu "
    "Altus). Where the texture names no area (the stacked Scadu Altus / Rauh Base "
    "band, unpainted map edges; #264) it is the map-name volume holding the spot, else "
    "the nearest name the texture paints within 128 m. location is the landmark "
    "whose MSB footprint holds it (Castle Morne, Caria Manor). In other maps, subarea is the map-name banner of the volume holding "
    "it when that differs from its location and region (Ainsel River Main, Nokron, "
    "Eternal City). An enemy that exists only in an NPC-invasion instance of the map "
    "carries world_state {kind: 'npc_invasion', ceremony, host, invader_flag} (#96; "
    "Old Knight Istvan, Millicent): it is absent from the normal world, where "
    "host is the instance's invader (see the location's invasion_instances). "
    "Returned, not searchable; filter on maps, "
    "regions, areas or locations. get_entity returns the list only when it has at most "
    "50 entries, else placements_total (include_placements=True for all)",
    "maps": "on enemy and item docs (#76): the distinct MSB map ids of its placements, "
    "e.g. maps='m10_00_00_00' finds everything placed in Stormveil Castle. DLC maps "
    "(m20-m28, m40-m45, m61) are resolved too",
    "regions": "on enemy and item docs (#140): the distinct map-menu regions of its "
    "placements (each placement's nearest grace) plus their tabs, so "
    "regions='Limgrave' also finds things in Stormhill and regions='Caelid' finds what "
    "is placed anywhere in Caelid. Every value is a location doc. Item regions cover "
    "pickups only; enemy drops are in drop_regions",
    "areas": "on enemy and item docs (#194): the distinct area values of its "
    "open-world placements (the game's own region map at each spot, #80), so "
    "areas='Stormhill' finds what is placed inside Stormhill's border even where the "
    "nearest grace is in Limgrave. No tabs are added (areas='Limgrave' doesn't match "
    "Stormhill) and dungeon placements have none. Item areas cover pickups only",
    "locations": "on enemy and item docs (#140): the dungeon location docs its "
    "placements are in (catacombs, caves, tunnels, gaols, legacy dungeons: "
    "locations='Murkwater Catacombs') and the open-world landmarks whose MSB footprint "
    "holds them (#80: locations='Castle Morne'). Item locations cover "
    "pickups only; enemy drops are in drop_locations. On a quest doc (#95): every "
    "step's locations (dungeons, open-world landmarks and regions), in step order",
    "drop_regions": "on item docs (#141): the regions (+ tabs) of the enemies in "
    "dropped_by, counting only the placements that carry the item (their own death "
    "lot or scripted award) when known, else every placement of that enemy. "
    "drop_regions='Caelid' finds what enemies in Caelid drop; kept apart from the "
    "pickup regions",
    "drop_locations": "on item docs (#141): the dungeon and landmark (#80) location "
    "docs of those dropping placements, e.g. drop_locations='Murkwater Catacombs'",
    "from": "on a warp doc (#94): where the warp starts. {map, world_position | "
    "position, grace, region, parent_region, location, entity_id}: entity_id is the "
    "MSB entity that triggers it (the waygate or stone asset the prompt is on, or a "
    "region/character the script checks), grace its nearest site_of_grace, region / "
    "parent_region that grace's, location the dungeon location doc (or, for an "
    "evergaol, the evergaol, with both ends taking the grace and region nearest "
    "its marker). On an open-world tile, location is the landmark whose MSB "
    "footprint holds the end (#190). A scripted warp with no placed trigger (a trap chest, "
    "a quest step in common scripts) has only the map of its script, or nothing; "
    "on an open-world tile its region is then that of the tile's only world-map "
    "landmark, which also names the warp (Dragon-Burnt Ruins -> Sellia Crystal "
    "Tunnel, Tower of Return -> Divine Bridge), else the tile centre's. Filter on "
    "from.grace / from.region / from.location / from.map; positions are returned, "
    "not searched",
    "to": "on a warp doc (#94): where the warp lands, same shape as from; "
    "entity_id is the destination player start (an MSB SpawnPoint region or Player "
    "part in the destination map), map the map the warp loads",
    "prompt": "on a warp doc (#94): the confirmation it asks (EventTextForMap): "
    "'Travel to another location?' (waygates), 'Return to entrance?', 'Enter "
    "evergaol?', 'Head to the realm of shadow?'. Absent when the warp doesn't ask",
    "gate_flag": "on a warp doc (#94): the event flag that must be on for a waygate "
    "to work; while it's off the gate says 'Cannot be used now' (Four Belfries "
    "gates, the Impassable Greatbridge gates: 9410). These are world-state flags "
    "(an Imbued Sword Key used, the Radahn festival begun), not quest steps; "
    "gate_set_when says what turns them on. A "
    "warp with a cutscene_id names its cutscene doc in cutscene (#189)",
    "gate_set_when": "on a warp doc (#228): what turns gate_flag on, read from the "
    "event scripts that set it: conditions shaped as quest steps.when, all required "
    "(The Four Belfries: item_held Imbued Sword Key + the map flag of that gate's "
    "keyhole + action_button 'Examine' on the keyhole; 9410, the Radahn festival: "
    "any_of(talk to Smithing Master Iji / Sorceress Sellen, Roderika's phase 3063) "
    "while no other festival flag 9411-9413 is on, with the player in their own "
    "world). An event that waits on a "
    "character's state adds it as kind character + state (dead, attacked, health, "
    "special_effect + special_effect_id, negated = the SpEffect absent, #251; npc / "
    "entity_ids, npc absent when the entity has no name: entity 10000 is the player, "
    "its alias 20000 is shown as 10000, #252), as quest outcome waited_for. Waits on the player's "
    "progress (#231): action_button (the player presses a prompt: action_button_id, "
    "prompt (its text, absent when the param row has none), entity_id the prompt "
    "is on), in_region (the player inside MSB region entity_id; negated = outside: "
    "map (when the MSB dumps place the region), plus locations of that map, area "
    "/ subarea / landmark of the region's position when they add a name), flag_range (first_flag..last_flag, "
    "range_state all_on / all_off / any_on / any_off), in_own_world (the player is "
    "the host, not a summoned phantom; negated = not in their own world; also when "
    "the event only runs on past a check of it, #237, or checks that the player's "
    "character type is Alive, i.e. not a phantom, #242), "
    "armor_equipped (items: the armor piece worn, else item_id; Head / Body / Arms "
    "/ Legs are the empty slots, i.e. no armor there), near_entity (#234: the "
    "player within distance of MSB entity_id; negated = beyond it; map / locations "
    "/ area / subarea / landmark as in_region; #243: also a distance to entity "
    "20000, which the scripts use for the player too: Yura's sign at Flying Dragon "
    "Agheel waits for her NPC near it), multiplayer_state (#245: the session's "
    "multiplayer state: host / client / multiplayer / multiplayer_pending / "
    "singleplayer / invasion / invasion_pending; negated = not in it; the Deeproot "
    "Depths waygate that sets the Isolated Divine Tower warp's gate flag 11000600 "
    "needs neither multiplayer nor multiplayer_pending). A wait on either of "
    "several such conditions, flags or character states (Queelign 2047462702: a "
    "flag or a flag range, each with a region; #244: 10010801, the player in their "
    "own world and a region, or the Grafted Scion attacked, kind character), or one "
    "only some of the event's paths take (the ending prompts), is "
    "an any_of of the alternatives, each one condition or an all_of (#234/#235; a "
    "path's alternative also holds the flags it set itself and the character it "
    "waited on; a path that only runs once a flag the event sets further on is "
    "already on is a re-run and adds no alternative). kind "
    "untracked_wait: the event also waits on something still not decoded (such as "
    "another character in a region, a value comparison, or more alternatives than "
    "fit), so the other conditions are needed but not "
    "enough on their own. When the setting events need different conditions beyond "
    "the shared ones, the last condition is an any_of of each event's extra: one "
    "condition, or an all_of of several (#233; an all_of holds plain conditions "
    "only, never another any_of, and an event needing a superset of another's "
    "extras adds no alternative). One hop only: a condition's "
    "own flag isn't expanded (kind flag, with set_at). Absent (unresolved) when an "
    "event sets the flag with no tracked or decoded condition (only area arrivals or "
    "untracked waits), when an event's extras hold an any_of plus other conditions, "
    "when one event waits on more than one character outside an OR group of decoded "
    "alternatives (#244), or past 12 conditions (or 12 all_of members, 8 any_of "
    "members; #245). A flag no event script sets but a talk script or "
    "item pickup does gets that one condition instead (#232): kind talk (the flag "
    "itself, npcs = the talking NPCs, absent when no talk script setting it belongs "
    "to a named NPC; scripts whose NPC number has no name are labelled by what they "
    "are (#239): Site of Grace (the grace menu), Melina (her talk 3000), Grand Lift of Rold (its "
    "medallion prompts), Church of Vows (absolution)) or item_pickup (items). A "
    "flag an event sets and a talk script "
    "or item pickup sets too gets that talk / item_pickup condition as one more "
    "any_of alternative beside the event's conditions (#238). An event that sets "
    "the flag only when it is already on adds nothing (#236). Also absent when "
    "nothing sets the flag by its literal id or as an event's own slot flag (event "
    "id + slot, #237), e.g. a computed flag. Same "
    "shape as "
    "npc_summons / hostile_signs requires_set_when and cutscene trigger_set_when",
    "event_id": "on a warp doc (#94): the EMEVD event that performs the warp: the "
    "common template for templated warps (90005605 waygate, 90005645/46 return to "
    "entrance, 90005880 evergaol exit, 90005881 evergaol enter), else the map "
    "script's own event",
    "warps_to": "on site_of_grace and location docs (#94): the places warps lead to "
    "from here (the destination's grace, else dungeon location, map name or region), "
    "e.g. The Four Belfries -> Dragon Temple, Worshippers' Woods, Chapel of "
    "Anticipation. A location matches warps whose start is in it (location, region "
    "or parent_region). Warps with both ends in one place (return to entrance, in "
    "and out of an evergaol) are left out; the warp docs themselves have the "
    "details",
    "warps_from": "on site_of_grace and location docs (#94): the places whose warps "
    "arrive here (reverse of warps_to)",
    "entity_id": "on a site_of_grace doc: the grace's MSB entity id (BonfireEntityId)",
    "unlock_flag": "on a site_of_grace doc: the event flag set when the grace is lit; "
    "on a tutorial game_text doc (#202): the TutorialParam UnlockEventFlagId of the "
    "row showing this text, the event flag that unlocks the tutorial (Sites of "
    "Grace: 710020; common event 1720 and map events like it wait on the flag, then "
    "show the tutorial's pop-up). Two tutorials can share one (Adding Skills and "
    "Adding Affinities: 710600). Absent when no TutorialParam row with a flag shows "
    "the text",
    "unlock_set_when": "on a tutorial game_text doc (#202): what turns unlock_flag "
    "on, shaped as warp gate_set_when: the event scripts' set_when (Sites of Grace: "
    "in_own_world + in_region of the Stranded Graveyard; 710050, #259: also item_held "
    "negated over 27 staffs and seals, i.e. holding none), plus the game's own "
    "setters as more any_of alternatives: item_acquired (items: the goods or Ashes "
    "of War whose ItemGetTutorialFlagId it is, a spirit ash's +N copies under its "
    "base name, cut names left out; Summoning Spirits: every spirit ash), "
    "enemy_killed (npc_param_ids: the NpcParam rows whose ChrDeadTutorialFlagId it "
    "is; Teardrop Scarabs), and three GameSystemCommonParam flags: telescope_view "
    "(TutorialFlagOnAccessDistView; Birdseye Telescopes), enemy_group_reward "
    "(TutorialFlagOnGetGroupReward; Vanquishing Enemy Groups) and "
    "spiritspring_region (TutorialFlagOnEnterRideJumpRegion; Spiritspring "
    "Jumping). Before patch 1.12 those three come from the current patch's table, "
    "which those patches' GameSystemCommonParam layout doesn't match. Absent when "
    "nothing found sets the flag (Multiplayer 710760, the untitled Stranded "
    "Graveyard tutorial 101120) or when an event sets it under conditions "
    "gate_set_when can't describe (the untitled 101050)",
    "bosses": "on a site_of_grace doc: the boss docs whose nearest grace it is; on a "
    "location doc: the boss docs in it (by region, or a dungeon's map)",
    "arena_position": "on a boss doc: the arena's position in its map's local "
    "coordinates (GameAreaParam BossPos; the boss's MSB placement when the param's "
    "BossMap is another map, e.g. Base Serpent Messmer's stale Haligtree row, or its "
    "dungeon BossPos is over 500 m from the boss, e.g. Ancestor Spirit's). In a dungeon "
    "it can sit up to ~200 m from where the boss stands",
    "runes": "on a boss doc: runes awarded for the kill (GameAreaParam "
    "SingleplayerSoulReward, before rune-gain buffs)",
    "banner": "on a boss doc: the defeat banner, i.e. the boss tier: Enemy Felled "
    "(field/dungeon bosses), Great Enemy Felled, Demigod Felled, Legend Felled, God Slain "
    "(Elden Beast, Consort Radahn), Duelist Vanquished. Absent when the defeat isn't "
    "scripted with a banner the event scripts expose",
    "defeat_flag": "on a boss doc: the event flag set when the boss is defeated "
    "(GameAreaParam DefeatBossFlagId)",
    "summon_stats": "on a spirit_ash doc: the summoned spirits' stats at +0, one entry "
    "per distinct spirit with count and the enemy stat groups (stats / defense / resistances "
    "/ immune_to / traits / weak_point_damage_multiplier), from BuddyParam -> NpcParam with "
    "the summon's upgrade-level SpEffect applied (at +0 most base-game spirits get double "
    "status resistance) on top of the spirit row's resident SpEffects (#181; the "
    "per-spirit balance effects since 1.13, e.g. Gravebird Ashes HP x1.3, Stormhawk "
    "Deenh x2.2). A filter like summon_stats.stats.hp matches if any spirit matches. "
    "Each spirit also has an attacks profile (summon_stats.attacks). Mimic Tear also "
    "lists its player-copy row. +10 in max_level, every level in upgrade_curve",
    "summon_stats.damage_multiplier": "spirit's outgoing damage multiplier at that upgrade "
    "level (1.0 at +0, ~3.8 at +10 for most base-game spirits); the resident per-element "
    "multipliers, including every spirit's x0.25 damage vs enemies (#248), are folded "
    "into attacks.attack_power instead. Mimic Tear's player copy has no attacks, so its "
    "damage vs enemies is the player's x damage_vs_enemies_multiplier (+10: 2.473 x 0.25 "
    "~= 0.62)",
    "summon_stats.damage_vs_enemies_multiplier": "the spirit's overall multiplier on "
    "damage dealt to enemies at that upgrade level (#254): damage_multiplier x the "
    "resident corrections that apply to every damage type alike (x0.25 on every spirit, "
    "SpEffect 296000, all patches; #248, times five spirits' own x0.7 to x1.5: Lhutel "
    "the Headless 0.312 at +0). Element-specific resident multipliers (e.g. "
    "Gravebird Ashes magic x1.65) are left out and appear only in attacks.attack_power. "
    "attacks.attack_power already includes it, so don't multiply the two. Mainly for "
    "Mimic Tear's player copy, which has no attacks: the player's damage x 0.25 at +0, "
    "x 0.618 at +10",
    "summon_stats.damage_taken_multiplier": "per damage type, the multiplier on damage "
    "the spirit takes from its resident SpEffects (#181), only where not 1.0: physical "
    "(standard) / strike / slash / pierce / magic / fire / lightning / holy. Every spirit "
    "takes x0.5 damage from enemies (SpEffect 296000, all patches; the Mimic Tear's "
    "observed flat 50% negation, #248), times its own cut rates: Crystalian Ashes 0.05 "
    "for everything but strike 0.5 (the crystal body), Bloodhound Knight Floh 0.3 "
    "physical",
    "summon_stats.poise_damage_taken_multiplier": "multiplier on poise (stance) damage "
    "the spirit takes, from its resident SpEffects (#249), only where not 1.0. 0.5 on "
    "almost every spirit since 1.13 (the patch that made spirits harder to stagger; that "
    "SpEffect also adds +50 to stats.poise; a few spirits' own resident effects add "
    "+50 to +70 more), Taylew the Golem Smith 1.72",
    "summon_stats.status_buildup_taken_multiplier": "per status, the multiplier on "
    "status buildup the spirit receives, from its resident SpEffects (#249), only where "
    "not 1.0; resistances are left as the raw threshold. Ancient Dragon Florissax 0.7 "
    "for every status since 1.13",
    "summon_stats.attacks": "spirit's attack profile, in the enemy attacks shape "
    "(behavior_variation / count / damage_types / elements / attack_power / "
    "poise_damage / status_buildup / status_effects; no shared_with) (#124). Spirits share their field "
    "enemy's move table (Lone Wolf Ashes = the Lone Wolf's), narrowed to the moves the "
    "spirit's animations fire, so AI-only sibling moves can remain. attack_power is "
    "scaled by that level's damage_multiplier and by the spirit's resident per-element "
    "damage multipliers: every spirit's x0.25 damage vs enemies (#248; five spirits "
    "carry their own x0.7 to x1.5 on top) and the 1.13+ balance effects (#181: "
    "Gravebird Ashes magic x1.65, 220 -> 91 at +0). Black Knife Tiche holy 62 at +0, "
    "237 at +10 in max_level.summon_stats.attacks; status_buildup is not scaled. "
    "poise_damage is x0.05 on every spirit (SpEffect 296000 saAttackPowerRate, all "
    "patches; #253) and does not change with level: Black Knife Tiche 90 -> 4.5, most "
    "spirits 0.5 to 1.5. "
    "Per-level attack_power is in upgrade_curve.summon_stats[i].attacks.attack_power. "
    "A spirit that spawns as a specific enemy variant keeps that variant's moves: "
    "Gravebird Ashes is the spectral-ring / poison-tail Gravebird (holy + poison), which "
    "the Gravebird doc lists under attacks.state_variants. Fingercreeper Ashes keeps its whole model-family table (no animation data "
    "for that model). Absent on player-copy and human spirits (Mimic Tear's copy, the Puppets, "
    "Jolán and Anna): they fight with equipped weapons. Mimic Tear's first entry is "
    "its Silver Tear form",
    "summon_count": "on a spirit_ash doc: total spirits summoned at +0 (e.g. Lone Wolf "
    "Ashes 3); max_level.summon_count at +10",
    "npc": "on a quest doc (#95): the NPC owning the flag block, named after the "
    "character its death / turned-hostile events bind, else the NPC whose talk scripts "
    "check the block most. An NPC with several blocks gets the flag range appended to "
    "the doc name, e.g. 'Moore (4380–4399)'. A different character sharing the NPC "
    "number keeps its own name (Lightseeker Hyetta, not Irina of Morne). A talk script "
    "with an unnamed NPC number counts under its #239 label (#247): Melina's talk 3000 "
    "names 4640–4659 'Melina', the grace menu (talk 1000) 4820–4839 'Site of Grace'; "
    "their name_ja is the JP FMG entry naming the same thing (NpcName 110000 メリナ, "
    "TutorialTitle 301020 祝福; #258). availability "
    "is 'cut' when the NPC's enemy doc is cut (Asimi, Silver Tear)",
    "npc_names": "on a quest doc: every NpcName persona of that NPC "
    "(['Heartbroken Maiden', 'Roderika', 'Roderika, Spirit Tuner'])",
    "flag_block": "on a quest doc: the NPC's 20 event flags [first, last]; +0..+4 are "
    "its life state, +5..+19 its quest phases. A second block of an NPC whose +1..+4 "
    "flags are set with no death / hostility evidence uses all 20 as phases "
    "(Roderika 3060, Moore 4920)",
    "related_npcs": "on a quest doc: other NPCs whose quest phase or life state gates "
    "one of its steps or outcomes",
    "steps": "on a quest doc: one entry per scripted phase transition (a phase set "
    "under different conditions has several entries), from the event scripts read "
    "control-flow aware (skips, gotos and returns on every path to the set) with each "
    "shared template read once per call site, its args substituted (so the Church of "
    "Vows absolution resets are gated on the absolution flag). The absolution also "
    "sets the +18 phase of nearly every NPC block from 3118 to 4718 (DLC NPCs "
    "included at 1.17), hostile or not, so those quest docs have a +18 step whose "
    "when is the absolution talk flag (Miriel). A when lists only "
    "conditions that must hold: a test that also involves unparsed checks (flag "
    "ranges, distances, value comparisons) contributes nothing it can't guarantee, "
    "so a when can be incomplete but not wrong. An either-or test of parsed checks "
    "is an any_of. A flag the event itself sets before the transition is not a "
    "condition (its earlier state is stale by then). Steps don't restate the "
    "NPC's own normal life state (+0) or 'not yet at phase X'. A phase that other "
    "scripts check but no parsed event "
    "sets (usually the first) is listed bare. Phases have no names: "
    "meaning is positional (order + locations + when)",
    "steps.phase_flag": "the phase's event flag (in flag_block)",
    "steps.order": "1-based depth of the phase in the transition chain (the longest "
    "entered_from path); sibling branches share an order (Millicent 4191 help vs 4194 "
    "betray)",
    "steps.entered_from": "the same NPC's phase the transition requires",
    "steps.locations": "where the phase plays out: the dungeon location docs, else the "
    "landmarks whose MSB footprint is centred on the open-world tile (#190) followed "
    "by the region of the grace nearest the tile's centre, of every map whose event "
    "script checks the phase flag",
    "steps.when": "the other conditions of the transition, all required. kind: "
    "boss_defeated (bosses), invasion (an NPC-invasion defeat flag, #138), item_pickup "
    "(items: the pickup lot's items), item_held (items; negated with several items: "
    "the player holds none of them, #259), talk (a flag the npcs' talk "
    "scripts set: a dialogue choice, hand-over or line, #257), quest_phase / life_state (npc, "
    "quest when that NPC has several blocks), hit_count (a map flag set once the "
    "player has hit the npc that many times while friendly: hits, entity_ids = its "
    "MSB entities; the hostility counter Ranni / Iji / Seluvis wait on), flag "
    "(unresolved; set_at = the "
    "locations of the maps whose events set it), or any_of (conditions: at least one "
    "of these nested conditions holds, each shaped as above). negated = the "
    "condition must be off",
    "outcomes": "on a quest doc: life-state changes: flag + slot (0-4) + life_state "
    "(hostile for +1/+2, dead for +3: labelled from the death / SetTeamType events "
    "that set them; +0/+4 unlabelled), with when (as steps.when) or trigger (death / "
    "attacked: the shared common event that sets it; event: an event script sets it "
    "after waiting for a character's state or another flag, listed in waited_for). "
    "The absolution at the Church of "
    "Vows shows as a +0 outcome when the absolution talk flag (Miriel) and the hostile "
    "state are on (the same absolution also sets the +18 phase, listed under steps)",
    "outcomes.trigger": "death (set when the character dies), attacked (set when "
    "attacking the NPC turns it hostile) or event (an event script sets it after "
    "waiting for a character's state or another flag, listed in waited_for: Sir "
    "Ansbach's +1 flag, set when Moore dies)",
    "outcomes.waited_for": "on an outcome with trigger event: what its event script "
    "waited for before setting the flag (any one of them, or several together, ends "
    "the wait). kind character = a character's state (state dead / alive / "
    "attacked / health / special_effect, with special_effect_id when one SpEffect "
    "is checked; npc = its name, entity_ids = its MSB entities; one entry per character, in the most telling state checked), else a "
    "condition shaped as steps.when. The NPC's own block flags are left out",
}


def describe_index(client: OpenSearch) -> dict:
    """Introspect the index: entity types, internal sources, and the field catalog.

    Lets a caller discover what is queryable without guessing. Field types come
    from the index mapping; entity_types and sources are counted live.
    """
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {
                "types": {"terms": {"field": "entity_type", "size": 50}},
                "sources": {"terms": {"field": "source", "size": 30}},
            },
        },
    )
    entity_types = {
        b["key"]: b["doc_count"] for b in resp["aggregations"]["types"]["buckets"]
    }
    sources = {
        b["key"]: b["doc_count"] for b in resp["aggregations"]["sources"]["buckets"]
    }

    fields: dict[str, dict] = {}

    def _walk(props: dict, prefix: str) -> None:
        for fname, spec in props.items():
            path = prefix + fname
            entry: dict = {"type": spec.get("type", "object")}
            subs = [s for s in spec.get("fields", {})]
            if subs:
                entry["subfields"] = subs
            if path in _FIELD_NOTES:
                entry["note"] = _FIELD_NOTES[path]
            fields[path] = entry
            if "properties" in spec:
                _walk(spec["properties"], path + ".")

    _walk(INDEX_MAPPING["mappings"]["properties"], "")

    return {"entity_types": entity_types, "sources": sources, "fields": fields}
