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

INDEX = "elden-ring-entities"

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
        "properties": {
            "entity_type": {"type": "keyword"},
            "name": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"},
                    "folded": {"type": "keyword", "normalizer": "ascii_normalizer"},
                },
            },
            "patch_version": {"type": "keyword"},
            "source": {"type": "keyword"},
            "description": {"type": "text"},
            "text_content": {"type": "text"},
            "tags": {"type": "keyword"},
            "location": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "weight": {"type": "float"},
            "attack_physical": {"type": "integer"},
            "attack_magic": {"type": "integer"},
            "attack_fire": {"type": "integer"},
            "attack_lightning": {"type": "integer"},
            "attack_holy": {"type": "integer"},
            "scaling_str": {"type": "keyword"},
            "scaling_dex": {"type": "keyword"},
            "scaling_int": {"type": "keyword"},
            "scaling_fai": {"type": "keyword"},
            "scaling_arc": {"type": "keyword"},
            "req_str": {"type": "integer"},
            "req_dex": {"type": "integer"},
            "req_int": {"type": "integer"},
            "req_fai": {"type": "integer"},
            "req_arc": {"type": "integer"},
            "fp_cost": {"type": "integer"},
            "slots": {"type": "integer"},
            "sort_id": {"type": "integer"},
            "menu_category": {"type": "keyword"},
            "npc_id": {"type": "keyword"},
            "name_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                },
            },
            "description_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                },
            },
            "text_content_ja": {
                "type": "text",
                "fields": {
                    "ja": {"type": "text", "analyzer": "kuromoji_analyzer"},
                    "morph": {"type": "text", "analyzer": "kuromoji_segmenter"},
                },
            },
            "acquisition_types": {"type": "keyword"},
            "acquisition_sources": {"type": "keyword"},
            "dropped_by": {"type": "keyword"},
            "sold_by": {"type": "keyword"},
            "base_item": {"type": "keyword"},
            "is_legendary": {"type": "boolean"},
            "achievement_set": {"type": "keyword"},
            "effect": {"type": "text"},
            "effect_value": {"type": "float"},
            "infusable": {"type": "boolean"},
            "default_ash_of_war": {"type": "keyword"},
            "depicted_in_talisman": {"type": "keyword"},
            "depicts_weapon": {"type": "keyword"},
        }
    },
}


def ensure_index(client: OpenSearch) -> None:
    if not client.indices.exists(index=INDEX):
        client.indices.create(index=INDEX, body=INDEX_MAPPING)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def analyze_text(client: OpenSearch, text: str) -> dict:
    """Return token streams for text under both indexed analyzers.

    Calls OpenSearch's _analyze API via the field path so the result reflects
    exactly what search_literal() applies: default CJK unigram on description_ja,
    kuromoji_segmenter on description_ja.morph.

    Returns {"standard": [...tokens...], "kuromoji_segmenter": [...tokens...]}
    """

    def _tokens(field: str) -> list[str]:
        resp = client.indices.analyze(index=INDEX, body={"field": field, "text": text})
        return [t["token"] for t in resp["tokens"]]

    return {
        "standard": _tokens("description_ja"),
        "kuromoji_segmenter": _tokens("description_ja.morph"),
    }


def search(
    client: OpenSearch,
    query: str,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    source: str | None = None,
) -> list[dict] | dict:
    filters = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})

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
        # the 16+ indexed patch versions. Relevance ordering is preserved; the representative
        # doc per entity is the highest-scoring version (identical content → deterministic).
        if not patch_version:
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


def get_entity(
    client: OpenSearch, name: str, entity_type: str | None = None
) -> dict | None:
    def _search(filters: list[dict]) -> list[dict]:
        resp = client.search(
            index=INDEX,
            body={
                "size": 1,
                "query": {"bool": {"filter": filters}},
                "sort": [{"patch_version": "desc"}],
            },
        )
        return resp["hits"]["hits"]

    filters: list[dict] = [{"term": {"name.keyword": name}}]
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    hits = _search(filters)
    if hits:
        return hits[0]["_source"]

    # Fallback: ASCII-fold + lowercase for diacritic-insensitive lookup
    # (e.g. "Misericorde" finds "Miséricorde")
    folded_filters: list[dict] = [{"term": {"name.folded": _ascii_fold(name)}}]
    if entity_type:
        folded_filters.append({"term": {"entity_type": entity_type}})
    hits = _search(folded_filters)
    return hits[0]["_source"] if hits else None


def _version_info(client: OpenSearch) -> dict:
    """Return loaded versions and dominant source per version in one query.

    Returns {"versions": [...sorted...], "sources": {"1.10.0": "erdb", ...}}
    """
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {
                "versions": {
                    "terms": {"field": "patch_version", "size": 30},
                    "aggs": {"top_source": {"terms": {"field": "source", "size": 1}}},
                }
            },
        },
    )
    buckets = resp["aggregations"]["versions"]["buckets"]
    versions = sorted(b["key"] for b in buckets)
    sources = {
        b["key"]: b["top_source"]["buckets"][0]["key"]
        for b in buckets
        if b["top_source"]["buckets"]
    }
    return {"versions": versions, "sources": sources}


def list_patch_versions(client: OpenSearch) -> list[str]:
    return _version_info(client)["versions"]


_DIFF_SKIP_FIELDS: frozenset[str] = frozenset(
    {"entity_type", "patch_version", "source", "npc_id"}
)


def diff_entities(
    client: OpenSearch,
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
    allow_cross_source: bool = False,
) -> dict:
    info = _version_info(client)
    loaded = set(info["versions"])

    for v in (v1, v2):
        if v not in loaded:
            return {
                "error": f"patch version '{v}' is not loaded; "
                f"loaded versions: {sorted(loaded)}"
            }

    if not allow_cross_source:
        src1 = info["sources"].get(v1)
        src2 = info["sources"].get(v2)
        if src1 and src2 and src1 != src2:
            return {
                "error": (
                    f"'{v1}' (source: {src1}) and '{v2}' (source: {src2}) are from "
                    f"different data sources — a diff measures scrape differences, not "
                    f"game revisions. Pass allow_cross_source=True to proceed anyway."
                )
            }

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

    doc1 = _fetch(v1)
    doc2 = _fetch(v2)

    if not doc1 or not doc2:
        # Distinguish "not in these patches" from "not in the index at all"
        missing = [v for v, d in ((v1, doc1), (v2, doc2)) if not d]
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
            return {"error": f"'{name}' not found in any loaded version"}
        missing_str = " and ".join(missing)
        return {"error": f"'{name}' not present in {missing_str}"}

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

    return {
        "name": name,
        "entity_type": (doc1 or doc2).get("entity_type"),
        "changed": bool(changed),
        "changed_fields": changed,
        "unchanged_fields": unchanged,
    }


_LITERAL_FIELDS = [
    "name",
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
    "description",
    "text_content",
    "name_ja.morph",
    "description_ja.morph",
    "text_content_ja.morph",
]


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
) -> dict:
    """Exact-phrase search across text fields, with optional structural filters.

    When patch_version is None (default): collapses by entity name and returns the
    latest version per entity; total reflects distinct entities, not raw index hits.
    When patch_version is specified: filters to that snapshot; total is the raw hit count.

    use_kuromoji routes Japanese fields through .morph subfields (kuromoji_segmenter:
    tokenizer-only, no lemmatization/stopwords/stemming) so phrase queries respect
    dictionary word boundaries. Single-kanji queries like 象 will not match 象徴 or 象牙.
    """
    search_fields = fields or (
        _LITERAL_FIELDS_MORPH if use_kuromoji else _LITERAL_FIELDS
    )
    filters: list[dict] = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})
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

    # Collect all phrase patterns (singular + list). OR them via bool.should.
    all_patterns: list[str] = []
    if pattern:
        all_patterns.append(pattern)
    if patterns:
        all_patterns.extend(patterns)

    must_clause: list[dict]
    if not all_patterns:
        must_clause = [{"match_all": {}}]
    elif len(all_patterns) == 1:
        must_clause = [
            {
                "multi_match": {
                    "query": all_patterns[0],
                    "fields": search_fields,
                    "type": "phrase",
                }
            }
        ]
    else:
        # OR across multiple patterns; dedup rides on cardinality agg + collapse.
        must_clause = [
            {
                "bool": {
                    "should": [
                        {
                            "multi_match": {
                                "query": p,
                                "fields": search_fields,
                                "type": "phrase",
                            }
                        }
                        for p in all_patterns
                    ],
                    "minimum_should_match": 1,
                }
            }
        ]

    body: dict = {
        "size": 0 if count_only else limit,
        "track_total_hits": True,
        "query": {
            "bool": {
                "must": must_clause,
                "filter": filters,
            }
        },
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
    allow_cross_source: bool = False,
) -> list[dict] | dict:
    """Return all entities of entity_type where field differs between v1 and v2.

    Fetches all entities for each version (up to 10 000 per call); comparison happens in Python.
    Only entities present in both versions are included (added/removed entities
    are excluded — use diff_entities for per-entity existence checks).
    """
    info = _version_info(client)
    loaded = set(info["versions"])

    for v in (v1, v2):
        if v not in loaded:
            return {
                "error": f"patch version '{v}' is not loaded; "
                f"loaded versions: {sorted(loaded)}"
            }

    if not allow_cross_source:
        src1 = info["sources"].get(v1)
        src2 = info["sources"].get(v2)
        if src1 and src2 and src1 != src2:
            return {
                "error": (
                    f"'{v1}' (source: {src1}) and '{v2}' (source: {src2}) are from "
                    f"different data sources — a comparison measures scrape differences, "
                    f"not game revisions. Pass allow_cross_source=True to proceed anyway."
                )
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
            hit["_source"]["name"]: hit["_source"].get(field)
            for hit in resp["hits"]["hits"]
        }

    docs_v1 = _fetch_all(v1)
    docs_v2 = _fetch_all(v2)

    results = []
    for name in sorted(set(docs_v1) & set(docs_v2)):
        val1 = docs_v1[name]
        val2 = docs_v2[name]
        if val1 != val2:
            results.append({"name": name, "text_before": val1, "text_after": val2})

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
