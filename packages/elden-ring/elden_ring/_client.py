"""OpenSearch client for self-hosted EC2 OpenSearch with basic auth + self-signed TLS."""

from __future__ import annotations

import os
import time
import unicodedata

import boto3
import requests
from opensearchpy import OpenSearch, RequestsHttpConnection

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
            # Weapon stats at its max upgrade (+25, somber +10) in the +0 shape, and
            # the per-level curve as non-indexed arrays (#112).
            "reinforce_type_id": {"type": "integer"},
            "max_level": {
                "properties": {"level": {"type": "integer"}, **_WEAPON_STATS}
            },
            "upgrade_curve": {"type": "object", "enabled": False},
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
            "base_item": {"type": "keyword"},
            "text_differs": {"type": "boolean"},
            "text_added_lines": {"type": "text"},
            "affinity": {"type": "keyword"},
            "is_legendary": {"type": "boolean"},
            "effect": {"type": "text"},
            "effect_value": {"type": "float"},
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


def _affinity_filter(collapse_affinity: bool) -> list[dict]:
    """Filter clause dropping non-Standard weapon affinity variants (#21).

    Infusable weapons are indexed once per affinity (Heavy Dagger, Keen Dagger, …),
    each stamped with ``affinity``. Collapsing keeps the Standard row; docs without the
    field (non-weapons, non-infusable weapons) pass through.
    """
    if not collapse_affinity:
        return []
    variant = {
        "bool": {
            "filter": [{"exists": {"field": "affinity"}}],
            "must_not": [{"term": {"affinity": "Standard"}}],
        }
    }
    return [{"bool": {"must_not": [variant]}}]


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
) -> list[dict] | dict:
    filters = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})
    filters += _availability_filter(include_unavailable)
    filters += _affinity_filter(collapse_affinity)

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


def get_entity(
    client: OpenSearch, name: str, entity_type: str | None = None
) -> dict | None:
    resolved = _resolve_entity_doc(client, name, entity_type)
    if resolved is None:
        return None
    doc, historical = resolved
    if not historical:
        return doc
    # A historical name matched an old patch; return the current doc instead of
    # stale stats, and say so (#63).
    newest = _newest_doc(client, {"name.keyword": doc["name"]}, doc["entity_type"])
    return {**(newest or doc), "name_is_historical": True, "queried_name": name}


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

    doc1, doc2 = _flatten(doc1), _flatten(doc2)
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
    filters += _affinity_filter(collapse_affinity)
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
    "entity_type": "category filter: weapon, armor, spell, item, ash_of_war, merchant, npc_dialogue",
    "patch_version": "real game patch the doc was extracted from (native is per-patch); use with diff_entities",
    "source": "internal game-data origin — the param table or FMG the doc was built from "
    "(EquipParamWeapon, EquipParamProtector, Magic, EquipParamAccessory, EquipParamGem, "
    "ShopLineupParam, TalkMsg). All data is first-party native extraction.",
    "availability": "'cut' for content whose in-game name row is [ERROR]-marked (scrapped, "
    "e.g. Millicent's set); 'unobtainable' for real-named armor with no acquisition path — "
    "enemy-only gear / reused assets like the Ragged set (#71); absent for normal obtainable "
    "content. Both flagged states are excluded from search by default — pass "
    "include_unavailable=True to include them.",
    "display_name": "per-patch in-game FMG name; differs from name when an item was renamed across patches",
    "menu_category": "in-game equipment menu grouping (e.g. 'Straight Sword', 'Reaper', 'Head')",
    "sort_id": "in-game sort index; base-game armaments are 1000-aligned with +N per affinity "
    "variant, but DLC bases are not 1000-aligned — use collapse_affinity, not sort_id_mod, "
    "to count distinct armaments",
    "affinity": "infusable weapon's affinity (Standard, Heavy, Keen, … Occult); each affinity "
    "is its own doc. Pass collapse_affinity=True to search tools to keep only Standard rows",
    "base_item": "rank variant's base (e.g. 'Erdtree's Favor' on 'Erdtree's Favor +2')",
    "text_differs": "on a talisman rank variant: its text diverges from base_item beyond the "
    "effect-magnitude rewording every rank has (Boosts → Greatly boosts, 上昇 → 大きく上昇 are "
    "ignored). False = only magnitude wording changed",
    "text_added_lines": "the variant's text lines (EN + JP) with no counterpart in base_item, "
    "e.g. 「伝説のタリスマン」のひとつ on Erdtree's Favor +2",
    "tags": "free-form keyword tags (spell school/role, weapon category, 'Talisman', etc.)",
    "location": "where a merchant is found",
    "sold_by": "merchant names that sell this item, derived per-patch from ShopLineupParam",
    "acquisition_types": "how the item is obtained, per-patch: merchant / enemy_drop / found_in_world",
    "acquisition_sources": "named sources: merchant names and/or boss/named-enemy names (see dropped_by)",
    "dropped_by": "enemies that drop this item: bosses/named enemies (map EMEVD + MSB "
    "placements, #68) and generic mobs named by their spirit-ash model label (#104)",
    "drops": "on an enemy doc: items this enemy drops (EMEVD awards + MSB death lots)",
    "name_source": "on an enemy doc: where the name comes from — 'npc_name' (the per-character "
    "NpcName roster) or 'spirit_ash' (a generic-mob model label taken from its spirit ash, "
    "e.g. 'Godrick Soldier'; covers every placement of that model, #104)",
    "chr_models": "on a spirit_ash enemy doc: the chr model ids the label covers (e.g. c4311). "
    "Also on a boss/creature roster doc (name_source=npc_name) whose name a spirit ash shares, "
    "e.g. Crystalian: its field mobs' drops are merged into that doc (#106)",
    "effect": "talisman/item effect text derived from SpEffectParam (native)",
    "effect_value": "primary numeric magnitude of the effect",
    "is_legendary": "part of a legendary set (achievement-tracked)",
    "infusable": "weapon can take an affinity/ash-of-war infusion",
    "default_ash_of_war": "the skill a weapon ships with (from SwordArtsParam)",
    "depicts_weapon": "talisman depicts this weapon (lore cross-reference)",
    "depicted_in_talisman": "weapon depicted in this talisman (lore cross-reference)",
    "attack_power": "weapon/ammo attack power at +0 by damage type, as shown in game "
    "(affinity multiplier applied). Weapons also carry stamina (damage dealt to the "
    "target's stamina) and critical (critical-hit multiplier, 100 = base; daggers 130)",
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
    "reinforce_type_id": "weapon's ReinforceParamWeapon type (the upgrade path; affinity "
    "types are 100-offset), kept for traceability",
    "max_level": "weapon stats at its max upgrade, in the same shape as the +0 fields "
    "(attack_power / scaling / guard, affinity applied); max_level.level is the max "
    "(+25 regular, +10 somber, 0 if it can't be upgraded). Sort on "
    "max_level.attack_power.physical for the strongest fully upgraded weapons",
    "upgrade_curve": "not searchable; returned by get_entity. The weapon's stats at every "
    "upgrade level as arrays indexed by level (upgrade_curve.attack_power.physical[25] = "
    "+25), only for stats that change with level; an absent stat keeps its +0 value",
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
    "stats.hp": "enemy base max HP (NpcParam, before per-area scaling)",
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
