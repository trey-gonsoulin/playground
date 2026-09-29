"""OpenSearch client for self-hosted EC2 OpenSearch with basic auth + self-signed TLS."""

from __future__ import annotations

import os
import time
import unicodedata

import boto3
import requests
from opensearchpy import OpenSearch, RequestsHttpConnection

from elden_ring._calc import STATS, attack_rating

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

# One spirit-ash summon entry (#86) at a given upgrade level (#117).
_SUMMON_STATS = {
    "count": {"type": "integer"},
    **_NPC_STATS,
    "damage_multiplier": {"type": "float"},
}


INDEX_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "tokenizer": {
                # Kuromoji in normal mode: dictionary-based segmentation with no
                # search-mode decompounding. Compounds like 象牙 stay as one token;
                # 象 never accidentally matches them.
                "kuromoji_normal": {
                    "type": "kuromoji_tokenizer",
                    "mode": "normal",
                }
            },
            "analyzer": {
                # Relevance analyzer for .ja subfields used by search(). Full filter
                # chain: baseform lemmatization, stopword removal, stemming.
                "kuromoji_analyzer": {
                    "type": "custom",
                    "tokenizer": "kuromoji_tokenizer",
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
                }
            },
            "status_effects": {"type": "keyword"},
            # Per-attack poise damage, first hit; chains stored, not indexed (#119).
            "poise_damage": {
                "properties": {**_POISE_HANDS, "pvp": {"properties": _POISE_HANDS}}
            },
            "poise_damage_chains": {"type": "object", "enabled": False},
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
                    "behavior_variation": {"type": "integer"},
                    "count": {"type": "integer"},
                    "damage_types": {"type": "keyword"},
                    "elements": {"type": "keyword"},
                    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
                    "status_buildup": {"properties": _props("integer", _STATUSES)},
                    "status_effects": {"type": "keyword"},
                    "shared_with": {"type": "keyword"},
                }
            },
            # Grab subset of that move table, joined to ThrowParam (#82).
            "grabs": {
                "properties": {
                    "count": {"type": "integer"},
                    "damage_types": {"type": "keyword"},
                    "elements": {"type": "keyword"},
                    "attack_power": {"properties": _props("integer", _DAMAGE_TYPES)},
                    "status_buildup": {"properties": _props("integer", _STATUSES)},
                    "status_effects": {"type": "keyword"},
                }
            },
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
            # In-game HP range over MSB placements, area scaling applied (#108).
            "hp_scaled": {
                "properties": _props("integer", ("min", "max", "placements"))
            },
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
                    "hp_scaled": {
                        "properties": _props("integer", ("min", "max", "placements"))
                    },
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
                    "hp_scaled": {"type": "integer"},
                    "ends_at_hp_ratio": {"type": "float"},
                    "hp_pool_shared_with": {"type": "keyword"},
                    "heals_on_entry": {"type": "boolean"},
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
            # Sites of grace (BonfireWarpParam, #78); region/map/nearest_grace shared
            # with bosses.
            "parent_region": {"type": "keyword"},
            "position": {"properties": _props("float", ("x", "y", "z"))},
            "world_position": {"properties": _props("float", ("x", "y", "z"))},
            "entity_id": {"type": "long"},
            "unlock_flag": {"type": "long"},
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
            # MSB placements of enemies and treasure pickups of items (#76): the
            # list is returned, never searched; maps is the filterable summary.
            "placements": {"type": "object", "enabled": False},
            "maps": {"type": "keyword"},
            # Placement region / dungeon location summaries (#140).
            "regions": {"type": "keyword"},
            "locations": {"type": "keyword"},
            "drop_regions": {"type": "keyword"},
            "drop_locations": {"type": "keyword"},
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
            # Spirit-ash summons: one entry per distinct summoned NpcParam row (#86).
            "summon_count": {"type": "integer"},
            "summon_stats": {"properties": _SUMMON_STATS},
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
            "acquisition_types": {"type": "keyword"},
            "acquisition_sources": {"type": "keyword"},
            "dropped_by": {"type": "keyword"},
            "drops": {"type": "keyword"},
            "sold_by": {"type": "keyword"},
            "shop_listings": {"type": "object", "enabled": False},  # #89
            "given_by": {"type": "keyword"},
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
            return {"total": resp["aggregations"]["distinct_entities"]["value"]}
        return {"total": resp["hits"]["total"]["value"]}

    return [{"score": hit["_score"], **hit["_source"]} for hit in resp["hits"]["hits"]]


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
    than ``limit``, keeping the count as ``placements_total``; maps, regions and
    locations still summarize where it is."""
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
) -> dict | None:
    resolved = _resolve_entity_doc(client, name, entity_type)
    if resolved is None:
        return None
    doc, historical = resolved
    if historical:
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
    "name_ja",
    "description_ja",
    "text_content_ja",
]
# Same as above but Japanese fields routed through the segmentation-only .morph
# subfield so phrase queries respect kuromoji morpheme boundaries.
_LITERAL_FIELDS_MORPH = [
    "name",
    "display_name",
    "description",
    "text_content",
    "name_ja.morph",
    "description_ja.morph",
    "text_content_ja.morph",
]
# Same as above but Japanese fields routed through the lemmatizing .lemma
# subfield (kuromoji_baseform only) so a baseform query matches all inflections.
_LITERAL_FIELDS_LEMMA = [
    "name",
    "display_name",
    "description",
    "text_content",
    "name_ja.lemma",
    "description_ja.lemma",
    "text_content_ja.lemma",
]
# Japanese text fields whose analyzer lives on a subfield, not the base field.
_JP_BASE_FIELDS = {"name_ja", "description_ja", "text_content_ja"}
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
    (name_ja, description_ja, text_content_ja) are routed to the matching .lemma/.morph
    subfield automatically, so fields=["description_ja"] with use_lemmatize=True searches
    description_ja.lemma. Without this, an explicit field would search the surface form and
    silently defeat the analyzer.
    """
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
            r = search_literal(
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
) -> dict:
    """Attack rating / status buildup / spell scaling of one weapon for character
    ``stats`` at ``level`` (default max), from its doc's ar_inputs (#120)."""
    bad = {s: v for s, v in stats.items() if not 1 <= v <= 99}
    if bad:
        return {"error": f"stats must be 1-99: {bad}"}
    name = weapon
    if affinity and affinity != "Standard" and not weapon.startswith(f"{affinity} "):
        name = f"{affinity} {weapon}"
    canonical = _resolve_entity_name(client, name, "weapon")
    if canonical is None and affinity:
        canonical = _affinity_variant(client, weapon, affinity)
    elif canonical is None and " " in weapon:  # "Heavy Scavenger's Curved Sword"
        canonical = _affinity_variant(client, *reversed(weapon.split(" ", 1)))
    if canonical is None:
        return {"error": f"weapon '{name}' not found"}
    versions = _entity_versions(client, "weapon")
    version = _resolve_asof(patch_version, versions) if patch_version else versions[-1]
    if version is None:
        return {"error": f"no weapon data at or before '{patch_version}'"}
    resp = client.search(
        index=INDEX,
        body={
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"name.keyword": canonical}},
                        {"term": {"entity_type": "weapon"}},
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
    return {
        "weapon": canonical,
        "patch_version": version,
        "level": level,
        "max_level": max_level,
        "two_handed": two_handed,
        "stats": full,
        "requirements": doc.get("requirements"),
        **attack_rating(inputs, doc.get("requirements") or {}, full, level, two_handed),
    }


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
    """
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
    "entity_type": "category filter: weapon, armor, spell, item, ash_of_war, merchant, "
    "enemy, boss (one doc per boss encounter, #79), npc_dialogue",
    "patch_version": "real game patch the doc was extracted from (native is per-patch); use with diff_entities",
    "source": "internal game-data origin — the param table or FMG the doc was built from "
    "(EquipParamWeapon, EquipParamProtector, Magic, EquipParamAccessory, EquipParamGem, "
    "ShopLineupParam, GameAreaParam, TalkMsg). All data is first-party native extraction.",
    "availability": "'cut' for content whose in-game name row is [ERROR]-marked (scrapped, "
    "e.g. Millicent's set); 'unobtainable' for real-named armor with no acquisition path — "
    "enemy-only gear / reused assets like the Ragged set (#71); absent for normal obtainable "
    "content. Both flagged states are excluded from search by default — pass "
    "include_unavailable=True to include them.",
    "display_name": "per-patch in-game FMG name; differs from name when an item was renamed across patches",
    "menu_category": "in-game equipment menu grouping (e.g. 'Straight Sword', 'Reaper', 'Head')",
    "sort_id": "in-game sort index; base-game armaments are 1000-aligned with +N per affinity "
    "variant, but DLC bases are not 1000-aligned — use collapse_variants, not sort_id_mod, "
    "to count distinct armaments",
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
    "tags": "free-form keyword tags (spell school/role, weapon category, 'Talisman', etc.)",
    "location": "where a merchant is found; on a boss doc, the legacy dungeon or area "
    "whose map holds the arena (PlaceName, e.g. Stormfoot Catacombs); absent for "
    "open-world bosses (see nearest_grace / region). Enemy and item placements name "
    "their dungeon location doc instead (see locations)",
    "sold_by": "merchant names that sell this item, derived per-patch from ShopLineupParam "
    "(includes Twin Maiden Husks for lineups they re-sell once given the bell bearing, #145)",
    "shop_listings": "not searchable; returned by get_entity. One entry per ShopLineupParam "
    "row selling the item (#89): vendor, condition (the shop row's unlock label, e.g. a "
    "scroll/prayerbook, quest step or nomadic merchant site), price, currency (runes / "
    "Dragon Heart at Dragon Communion / Starlight Shards for Seluvis's puppets / Heart of "
    "Bayle at the Grand Altar; else the raw cost_type), quantity (stock; "
    "absent = unlimited), unlock_flag (the event flag that makes the row visible, e.g. a "
    "bell bearing handed to the Twin Maiden Husks; absent = always sold), unlocked_by (the "
    "goods that set unlock_flag: handed over in the vendor's talk script — bell bearings, "
    "scrolls, prayerbooks, quest items like Seluvis's Potion — or picked up, #144), "
    "handed_to (the NPCs whose talk script takes that item: Seluvis's Potion given to "
    "Nepheli unlocks his Dolores puppet, given to the Dung Eater his Dung Eater puppet; "
    "Valkyrie's Prosthesis given to Millicent unlocks Gowry's Pest Threads), "
    "unlocked_by_defeating (bosses whose defeat sets it: Enia's remembrance-boss rows, "
    "Dragon Communion's dragons, #144; a flag with neither is a quest step), resold_from + "
    "resale_flags on the Twin Maiden Husks' copies of another merchant's rows (#145: given "
    "a merchant's bell bearing, the Husks sell that lineup; unlocked_by / handed_to then "
    "cover both the bell bearing and the row's own unlock, e.g. Pest Threads resold from "
    "Gowry needs Gowry's Bell Bearing and Valkyrie's Prosthesis) and materials "
    "[{item, quantity}] for item costs (remembrance trades; some also charge runes, e.g. "
    "Grafted Dragon 2000 + Remembrance of the Grafted). E.g. Somber Smithing Stone [9]: "
    "Twin Maiden Husks, 25000 runes, unlimited, unlock_flag 11109759, unlocked_by "
    "[Somberstone Miner's Bell Bearing [5]]",
    "acquisition_types": "how the item is obtained, per-patch: merchant / enemy_drop / "
    "found_in_world / chest (a treasure-chest placement, #76) / given_by_npc (an NPC's talk "
    "script gives it, see given_by) / starting_equipment (a starting class's gear or item, "
    "see starting_classes) / keepsake (on the character-creation keepsake menu) (#23) / "
    "crafted (has a crafting recipe, see crafted_from, #87) / gathered (a gathering-node "
    "pickup such as a herb, butterfly or ore; placements with gathering, #142) / "
    "quest_reward (a map event script awards it once a quest flag is set, e.g. Rogier's "
    "Bell Bearing, the Volcano Manor rewards; #137) / invader_drop (awarded for defeating "
    "an NPC invader, e.g. Hoslow's Petal Whip, Millicent's Prosthesis; #138). Scripted "
    "awards that wait only on an interaction (paintings, Great Rune restoration) carry "
    "neither",
    "acquisition_sources": "named sources: merchant names, boss/named-enemy names (see "
    "dropped_by) and gift-giving NPC names (see given_by)",
    "given_by": "on an item doc: NPCs whose talk script gives the item (#23), named via the "
    "NPC's map placement. One script can serve several personas of the same character "
    "(Roderika / Roderika, Spirit Tuner) or a shared questline (Irina and Hyetta); gifts from "
    "scripts with no named placement (Melina, some DLC characters) have acquisition_types "
    "given_by_npc but no name. DLC gifts are indexed at 1.17.0 only (the older DLC-era "
    "patches' DLC talk scripts aren't extracted)",
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
    "placements, #68) and generic mobs named by their spirit-ash model label (#104)",
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
    "name_source": "on an enemy doc: where the name comes from — 'npc_name' (the per-character "
    "NpcName roster) or 'spirit_ash' (a generic-mob model label taken from its spirit ash, "
    "e.g. 'Godrick Soldier'; covers every placement of that model, #104)",
    "chr_models": "on a spirit_ash enemy doc: the chr model ids the label covers (e.g. c4311). "
    "Also on a boss/creature roster doc (name_source=npc_name) whose name a spirit ash shares, "
    "e.g. Crystalian: its field mobs' drops are merged into that doc (#106)",
    "effect": "readable effect text decoded from SpEffectParam, one phrase per effects "
    "entry (e.g. Golden Vow: '+15% attack; -10% damage taken for 80s'). On talismans, "
    "consumables, crystal tears and spells (#88); a spell's or throwable's includes what its "
    "projectile inflicts on hit (e.g. '+100 frostbite buildup')",
    "effect_value": "primary numeric magnitude of the effect (the first effects entry's value)",
    "effects": "structured effects decoded from SpEffectParam (#88), one entry per stat: "
    "stat (e.g. 'attack' = every element, 'physical damage taken', 'HP restored', 'max HP "
    "restored', 'runes', 'immunity' / 'robustness' / 'focus' = both resistances of that "
    "group, 'poison buildup', 'poison cured'), value (signed; % or points per unit, "
    "absent for cure / inflict), unit ('%' or 'points'), pvp_value (the value against "
    "players when it differs, e.g. Exalted Flesh 20 / 15), condition (the attacks or state "
    "it's limited to: 'charged attacks', 'jump attacks', 'skills', 'at full HP', 'HP at or "
    "below 20%', 'on hit', 'successive attacks'; stacking effects give one entry per tier, "
    "'successive attacks, tier 1' … 'tier 3', each value the tier's total, e.g. Winged "
    "Sword Insignia +3 / +5 / +10%; the bonus decays ~1.5s after the last hit), interval (seconds between regen / drain ticks), duration (seconds; "
    "absent = instant or while equipped), target ('Torrent' for raisins / horse effects), "
    "scales_with ('faith' / 'intelligence' for heals that scale). Only confirmed fields are "
    "decoded, so some effects are missing (great runes, spells whose "
    "effect comes from a child projectile)",
    "effect_duration": "longest effects duration in seconds (absent = instant or permanent)",
    "is_legendary": "part of a legendary set (achievement-tracked)",
    "infusable": "weapon can take an affinity/ash-of-war infusion",
    "default_ash_of_war": "the skill a weapon ships with (from SwordArtsParam)",
    "depicts_weapon": "talisman depicts this weapon (lore cross-reference)",
    "depicted_in_talisman": "weapon depicted in this talisman (lore cross-reference)",
    "attack_power": "weapon/ammo attack power at +0 by damage type, as shown in game "
    "(affinity multiplier applied). Weapons also carry stamina (damage dealt to the "
    "target's stamina) and critical (critical-hit multiplier, 100 = base; daggers 130). "
    "Ammo carries every element it deals (Fire Arrow physical 15 + fire 95)",
    "projectile": "ammo standard-shot flight, from its Bullet param (#91; bow skills "
    "like Mighty Shot use other bullets): speed / max_speed (m/s), range (metres "
    "flown before the shot starts to drop: Fletched bone arrows 30 vs 10), gravity "
    "(drop after range), lifetime (s), hit_radius (m). follow_up_hits counts the extra "
    "hits it spawns on impact (explosions, shockwaves, lightning strikes: Golem's Great "
    "Arrow, Explosive Bolt, Lightning Greatbolt), follow_up_attack_power their flat "
    "added power by element (Explosive Greatbolt fire 180) on top of the ammo's own",
    "status_effects": "ammo: statuses a shot inflicts, from the ammo's and its bullets' "
    "on-hit SpEffects including damage-over-time chains (Rotbone Arrow: scarlet_rot); "
    "the buildup amount is in status_buildup",
    "guard": "weapon block stats at +0, affinity multiplier applied: guarded damage "
    "negation % by type (physical/magic/fire/lightning/holy), boost (guard boost) and "
    "resistances (guarded status resistance by status)",
    "guard.boost": "guard boost: how well blocking withstands stamina damage",
    "guard.resistances": "guarded status buildup resistance: poison / scarlet_rot / bleed / "
    "frostbite / sleep / madness / death_blight (the in-game Guard 'Resist' line)",
    "scaling": "weapon attribute scaling at +0 by stat (str/dex/int/fai/arc), affinity "
    "multiplier applied; scaling.<stat>.grade is the in-game letter (S>=175 A>=140 B>=90 "
    "C>=60 D>=25 E>=1), scaling.<stat>.value the number it is graded from",
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
    "War / weapon skill attacks are not included. Full hit chains in "
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
    "upgrade_curve.summon_count when the number of spirits grows (Giant Rat Ashes 3 -> 5). "
    "Weapons and spirit ashes also carry upgrade_curve.materials: the materials to reach "
    "each level as [{item, quantity}] (index 0 is null; Dagger materials[25] = Ancient "
    "Dragon Smithing Stone x1, Black Knife Tiche materials[1] = Ghost Glovewort [1]) (#87), "
    "and upgrade_curve.rune_cost: the runes to reach each level (index 0 is null; "
    "ReinforcePrice x the level's rate, somber's last step x6 after x5; Longsword "
    "rune_cost[25] = 1450, Moonveil rune_cost[10] = 2160, Black Knife Tiche "
    "rune_cost[10] = 14000; absent when the price is 0, e.g. Giant Rat Ashes) (#143)",
    "ar_inputs": "not searchable and not returned by get_entity: a weapon's per-level "
    "attack / scaling / buildup and correction curves, the inputs calculate_attack_rating "
    "uses to compute attack rating and Arcane status buildup for given stats",
    "requirements": "attribute requirements by stat (weapons: str/dex/int/fai/arc; spells: "
    "int/fai)",
    "negation": "armor damage negation % by type: physical, strike, slash, pierce (physical "
    "sub-types), magic, fire, lightning, holy",
    "alterable": "armor piece can be altered (Boc / Master Hewg service)",
    "altered_variant": "name of the altered version of this armor",
    "altered_from": "name of the base armor this piece is altered from",
    "name_ja": "Japanese name; .ja/.morph/.lemma subfields drive JP search modes",
    "description_ja": "Japanese description; .ja/.morph/.lemma subfields drive JP search modes",
    "text_content_ja": "Japanese long text; .ja/.morph/.lemma subfields drive JP search modes",
    "npc_id": "enemy's NpcName FMG id (6-digit humanoid / 9-digit boss & creature)",
    "stats": "enemy combat stats from one NpcParam row, bound by boss health bar, then "
    "NameID, then spirit-ash label (#84)",
    "stats.hp": "enemy base max HP (NpcParam, before per-area scaling; the in-game HP is "
    "hp_scaled)",
    "stats.poise": "enemy max poise; absent when poise is disabled",
    "defense": "enemy elemental defense (NpcParam): magic, fire, lightning, holy. NpcParam has "
    "no physical defense",
    "resistances": "enemy status buildup resistances (NpcParam): poison / scarlet_rot / bleed "
    "/ frostbite / sleep / madness / death_blight. 999 = immune; higher = more buildup needed",
    "immune_to": "enemy statuses at 999 resistance (immune), e.g. madness, death_blight",
    "traits": "enemy weakness classes from NpcParam flags: weak_to_gravity (bonus damage from "
    "gravity weapons), lives_in_death (Golden Order weapons), ancient_dragon, dragon "
    "(dragon-slaying weapons), undead. Empty list = none; absent = no NpcParam row bound",
    "weak_point_damage_multiplier": "enemy damage multiplier on hits to weak body parts",
    "attacks": "enemy attack profile (#81), aggregated over the move table of its NpcParam "
    "BehaviorVariationID (BehaviorParam -> AtkParam_Npc / Bullet -> on-hit SpEffect). A table "
    "belongs to a whole model family, so this is a SUPERSET of what this enemy uses; "
    "shared_with names the other enemies on the same table (Commander Niall lists "
    "Commander O'Neil's scarlet rot). Moves have no names in the data, so there is no "
    "per-move list. Absent on humanoid NPCs/invaders, which fight with equipped weapons",
    "attacks.attack_power": "max base attack power per element across the table (before "
    "per-area scaling; includes grabs and set-piece attacks)",
    "attacks.elements": "elements any move deals: physical / magic / fire / lightning / holy",
    "attacks.damage_types": "physical damage types across moves: Slash / Strike / Pierce / "
    "Standard",
    "attacks.status_buildup": "max direct per-hit status buildup per status",
    "attacks.status_effects": "statuses any move inflicts, including damage-over-time "
    "effects with no per-hit value (Mohg's bloodflame bleed); filter here for 'which "
    "enemies inflict X'",
    "attacks.shared_with": "other enemies sharing this move table; non-empty means the "
    "profile may include their moves",
    "attacks.count": "distinct attack + projectile rows reached from the table",
    "attacks.behavior_variation": "NpcParam BehaviorVariationID (the move table id)",
    "grabs": "enemy grab attacks on the player (#82): the moves of the same move table as "
    "attacks whose catch hit starts a ThrowParam grab, plus the hits dealt during the "
    "throw. Like attacks, a model-family SUPERSET (see attacks.shared_with). Absent = "
    "no grab. Filter exists:grabs for 'which enemies can grab you'",
    "grabs.count": "distinct grabs (ThrowParam rows for this enemy's model)",
    "grabs.attack_power": "max base attack power per element across grab hits (before "
    "per-area scaling)",
    "grabs.elements": "elements any grab hit deals: physical / magic / fire / lightning / "
    "holy",
    "grabs.damage_types": "physical damage types of grab hits: Slash / Strike / Pierce / "
    "Standard",
    "grabs.status_buildup": "max direct per-hit status buildup of grab hits",
    "grabs.status_effects": "statuses a grab inflicts (Margit's grab: bleed)",
    "team_type": "enemy NpcParam TeamType (#83): the team deciding whose attacks hit whom and "
    "who is targeted, so enemies with the same value are allies. Values without a team "
    "label are Elden Ring factions with no confirmed name (48 = lord soldiers + Mad "
    "Pumpkin Heads, 51 = demi-humans / imps / albinaurics, 11 = dragons, 9 = Runebear / "
    "trolls)",
    "team": "readable team_type label where confirmed: enemy (6), boss (7, health-bar field "
    "bosses), arch_enemy (33, marquee bosses like Malenia and Radahn), friendly_npc (26), "
    "hostile_npc (27, invaders), none (0, untargetable NPCs/objects), cooperator (2, "
    "summonable-ally rows), spirit_summon (47)",
    "ai": "enemy AI perception profile (#83) from NpcThinkParam, the think row most used by "
    "the enemy's MSB placements. Absent = never placed with a think row",
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
    "hp_scaled": "enemy in-game max HP (#108) over its MSB placements: each placement's "
    "NpcParam row is base HP times its SpEffect HP multipliers (the per-area scaling, plus "
    "e.g. x2 on field-boss versions of regular enemies), floored after each. First "
    "playthrough, solo: NG+ and multiplayer scaling are not applied. A phase-2 boss "
    "sharing phase 1's character shows phase 1's HP; see phases for per-phase HP. "
    "Absent = never placed",
    "hp_scaled.min": "lowest in-game HP over the enemy's placements",
    "hp_scaled.max": "highest in-game HP over the enemy's placements (min = max when one "
    "encounter or all placements scale alike)",
    "hp_scaled.placements": "number of MSB placements the range covers",
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
    "weak_point_damage_multiplier) plus hp_scaled; a filter like variants.stats.hp matches "
    "if any variant matches",
    "variants.name": "item variant doc's name (get_entity it for the full doc)",
    "variants.affinity": "weapon variant's affinity (Heavy, Keen, … Occult)",
    "variants.rank": "talisman / flask variant's +N rank",
    "variants.differs": "top-level fields whose values differ from the base (stats, text, "
    "guard, max_level, poise_damage, …; names, sort order and acquisition are not "
    "compared). Only compact fields carry values in the entry",
    "variants.npc_ids": "NpcName ids whose health bar shows this stat block",
    "variants.npc_param_ids": "NpcParam rows in this stat block",
    "variants.hp_scaled": "in-game HP range over this block's placements (as hp_scaled)",
    "variants.maps": "MSB map ids of this block's placements (e.g. m12_01_00_00 for the "
    "Lake of Rot). DLC maps are omitted: their MSB file names are content hashes",
    "phases": "multi-phase boss fight (#132), the same list on every phase's doc: one "
    "entry per fighting character in phase order, from the map event scripts' boss "
    "events, e.g. Beast Clergyman -> Maliketh, Radagon -> Elden Beast, Godfrey -> "
    "Hoarah Loux, Malenia's two bars, Rennala's two phases, Fia's Champions -> Rogier -> "
    "Lionel. Each entry has the enemy stat groups of that character's placed NpcParam "
    "row (stats / defense / resistances / immune_to / traits / "
    "weak_point_damage_multiplier) plus hp_scaled. Total HP to beat the fight: sum "
    "hp_scaled x (1 - ends_at_hp_ratio) over the entries, counting a shared HP pool once "
    "(skip entries with hp_pool_shared_with; the pool's own entry carries it). Absent = "
    "one-phase fight, a duo (co-bosses on one bar event), or a hand-off the scripts "
    "don't express as an HP check (Morgott)",
    "phases.phase": "1-based phase number; co-bosses in one phase share it (Lionel and "
    "two Fia's Champions)",
    "phases.name": "health-bar name of this phase's character",
    "phases.npc_id": "NpcName id shown on this phase's health bar",
    "phases.npc_param_id": "NpcParam row of this phase's fighting character",
    "phases.hp_scaled": "this character's in-game max HP (area scaling applied, as "
    "hp_scaled)",
    "phases.ends_at_hp_ratio": "HP ratio at which the fight moves to the next phase: "
    "0.55 = at 55% HP left (Beast Clergyman), 0.0 = on death. Absent on the last phase or "
    "when the hand-off waits on something other than this character's HP",
    "phases.hp_pool_shared_with": "the phase whose HP pool this character's damage also "
    "drains, so both bars are one pool (Godfrey's damage feeds Hoarah Loux's 21,903)",
    "phases.heals_on_entry": "the phase starts with a scripted regeneration effect "
    "(Malenia, Goddess of Rot). How far it heals isn't in the params or event scripts "
    "(the wiki says 80%), so hp_scaled is the full max HP",
    "enemies": "on a boss doc: the enemy doc names fought in this encounter, from its "
    "health bars (every phase and duo partner: Godfrey + Hoarah Loux, Radagon + Elden "
    "Beast, the two Night's Cavalry). The boss doc's name is the defeated character's "
    "(the last phase); a name shared by several encounters of the patch gets the place "
    "in parentheses: 'Night's Cavalry (Gate Town Bridge)', 'Erdtree Burial Watchdog "
    "(Stormfoot Catacombs)'",
    "boss_encounters": "on an enemy doc: the boss docs where it is fought (reverse of "
    "enemies)",
    "region": "on a site_of_grace doc: the grace's map-menu region (Stormhill, Liurnia "
    "of the Lakes, Leyndell, Ashen Capital, Gravesite Plain); on a boss doc and a "
    "location marker doc: the region of its nearest grace. Every region value is also "
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
    "label (unique landmarks). A marker named like a region merges into the region doc",
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
    "(19351-19370) are left out; open-world landmarks use their region's tiers",
    "nearest_grace": "on a boss doc: the site_of_grace doc closest to the arena (world "
    "coordinates in the open world, same map otherwise); locates open-world bosses",
    "map": "MSB map id: a boss doc's arena, a site_of_grace doc's map (a dungeon grace "
    "gives the dungeon's map even though its world-map marker is on the overworld), a "
    "location marker's map (a dungeon's own map, likewise). "
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
    "world_position | position, lot_id, in_chest}; in_chest is true only for "
    "treasure chests (altar and tree pickups aren't chests); plus one entry per "
    "gathering node (an herb, flower, butterfly, mushroom or ore asset whose model "
    "carries the pickup lot, #135), {map, world_position | position, lot_id, "
    "gathering: true}, so common materials have thousands; enemy drops are not "
    "pickups (see dropped_by). Open-world tiles (m60 base, m61 DLC, any tile size) give "
    "world_position; dungeons and legacy maps give map-local position. Each entry also "
    "carries region / parent_region (its nearest grace's, #140) and, in a dungeon map, "
    "location (the dungeon's location doc). Returned, not searchable; filter on maps, "
    "regions or locations. get_entity returns the list only when it has at most 50 "
    "entries, else placements_total (include_placements=True for all)",
    "maps": "on enemy and item docs (#76): the distinct MSB map ids of its placements, "
    "e.g. maps='m10_00_00_00' finds everything placed in Stormveil Castle. DLC maps "
    "(m20-m28, m40-m45, m61) are resolved too",
    "regions": "on enemy and item docs (#140): the distinct map-menu regions of its "
    "placements (each placement's nearest grace) plus their tabs, so "
    "regions='Limgrave' also finds things in Stormhill and regions='Caelid' finds what "
    "is placed anywhere in Caelid. Every value is a location doc. Item regions cover "
    "pickups only; enemy drops are in drop_regions",
    "locations": "on enemy and item docs (#140): the dungeon location docs its "
    "placements are in (catacombs, caves, tunnels, gaols, legacy dungeons: "
    "locations='Murkwater Catacombs'). Open-world landmarks (ruins, forts) have no "
    "footprint, so open-world placements only get regions. Item locations cover "
    "pickups only; enemy drops are in drop_locations",
    "drop_regions": "on item docs (#141): the regions (+ tabs) of the enemies in "
    "dropped_by, counting only the placements that carry the item (their own death "
    "lot or scripted award) when known, else every placement of that enemy. "
    "drop_regions='Caelid' finds what enemies in Caelid drop; kept apart from the "
    "pickup regions",
    "drop_locations": "on item docs (#141): the dungeon location docs of those "
    "dropping placements, e.g. drop_locations='Murkwater Catacombs'",
    "entity_id": "on a site_of_grace doc: the grace's MSB entity id (BonfireEntityId)",
    "unlock_flag": "on a site_of_grace doc: the event flag set when the grace is lit",
    "bosses": "on a site_of_grace doc: the boss docs whose nearest grace it is; on a "
    "location doc: the boss docs in it (by region, or a dungeon's map)",
    "arena_position": "on a boss doc: the arena's position in its map's local "
    "coordinates (GameAreaParam BossPos)",
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
    "status resistance). A filter like summon_stats.stats.hp matches if any spirit matches. "
    "No attack power: summon damage lives per attack, not on the NpcParam row "
    "(damage_multiplier scales it). Mimic Tear also lists its player-copy row. +10 in "
    "max_level, every level in upgrade_curve",
    "summon_stats.damage_multiplier": "spirit's outgoing damage multiplier at that upgrade "
    "level (1.0 at +0, ~3.8 at +10 for most base-game spirits)",
    "summon_count": "on a spirit_ash doc: total spirits summoned at +0 (e.g. Lone Wolf "
    "Ashes 3); max_level.summon_count at +10",
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
