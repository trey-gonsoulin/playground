"""OpenSearch client for self-hosted EC2 OpenSearch with basic auth + self-signed TLS."""

from __future__ import annotations

import os
import time

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
            "analyzer": {
                # Kuromoji morphological analyzer for Japanese relevance search.
                # Standard analyzer (the default) remains on the primary ja fields so
                # search_literal() phrase queries still do exact CJK-unigram substring
                # matching. This analyzer powers the .ja sub-fields used only by search().
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
                }
            }
        },
    },
    "mappings": {
        "properties": {
            "entity_type":      {"type": "keyword"},
            "name":             {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "patch_version":    {"type": "keyword"},
            "source":           {"type": "keyword"},
            "description":      {"type": "text"},
            "text_content":     {"type": "text"},
            "tags":             {"type": "keyword"},
            "location":         {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "weight":           {"type": "float"},
            "attack_physical":  {"type": "integer"},
            "attack_magic":     {"type": "integer"},
            "attack_fire":      {"type": "integer"},
            "attack_lightning": {"type": "integer"},
            "attack_holy":      {"type": "integer"},
            "scaling_str":      {"type": "keyword"},
            "scaling_dex":      {"type": "keyword"},
            "scaling_int":      {"type": "keyword"},
            "scaling_fai":      {"type": "keyword"},
            "scaling_arc":      {"type": "keyword"},
            "req_str":          {"type": "integer"},
            "req_dex":          {"type": "integer"},
            "req_int":          {"type": "integer"},
            "req_fai":          {"type": "integer"},
            "req_arc":          {"type": "integer"},
            "fp_cost":          {"type": "integer"},
            "slots":            {"type": "integer"},
            "sort_id":          {"type": "integer"},
            "menu_category":    {"type": "keyword"},
            "npc_id":             {"type": "keyword"},
            "name_ja":            {"type": "text", "fields": {"ja": {"type": "text", "analyzer": "kuromoji_analyzer"}}},
            "description_ja":     {"type": "text", "fields": {"ja": {"type": "text", "analyzer": "kuromoji_analyzer"}}},
            "text_content_ja":    {"type": "text", "fields": {"ja": {"type": "text", "analyzer": "kuromoji_analyzer"}}},
            "acquisition_types":  {"type": "keyword"},
            "acquisition_sources": {"type": "keyword"},
            "dropped_by":          {"type": "keyword"},
            "sold_by":             {"type": "keyword"},
            "base_item":           {"type": "keyword"},
            "is_legendary":        {"type": "boolean"},
            "achievement_set":     {"type": "keyword"},
            "effect":              {"type": "text"},
            "effect_value":        {"type": "float"},
        }
    },
}


def ensure_index(client: OpenSearch) -> None:
    if not client.indices.exists(index=INDEX):
        client.indices.create(index=INDEX, body=INDEX_MAPPING)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def search(
    client: OpenSearch,
    query: str,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
) -> list[dict]:
    filters = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})

    body: dict = {
        "size": limit,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "name^3", "name_ja^3", "name_ja.ja^3",
                                "description", "description_ja", "description_ja.ja",
                                "text_content", "text_content_ja", "text_content_ja.ja",
                                "location^1.5", "tags^2",
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

    # Without a specific patch filter, collapse by entity name to deduplicate across
    # the 16+ indexed patch versions. Relevance ordering is preserved; the representative
    # doc per entity is the highest-scoring version (identical content → deterministic).
    if not patch_version:
        body["collapse"] = {"field": "name.keyword"}

    resp = client.search(index=INDEX, body=body)
    return [{"score": hit["_score"], **hit["_source"]} for hit in resp["hits"]["hits"]]


def get_entity(client: OpenSearch, name: str, entity_type: str | None = None) -> dict | None:
    filters: list[dict] = [{"term": {"name.keyword": name}}]
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})

    resp = client.search(
        index=INDEX,
        body={"size": 1, "query": {"bool": {"filter": filters}}},
    )
    hits = resp["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def list_patch_versions(client: OpenSearch) -> list[str]:
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {"versions": {"terms": {"field": "patch_version", "size": 30}}},
        },
    )
    return sorted(b["key"] for b in resp["aggregations"]["versions"]["buckets"])


_DIFF_SKIP_FIELDS: frozenset[str] = frozenset({"entity_type", "patch_version", "source", "npc_id"})


def diff_entities(
    client: OpenSearch,
    name: str,
    v1: str,
    v2: str,
    entity_type: str | None = None,
) -> dict:
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

    if not doc1 and not doc2:
        return {"error": f"'{name}' not found in {v1} or {v2}"}
    if not doc1:
        return {"error": f"'{name}' not found in {v1} — may not exist in that patch"}
    if not doc2:
        return {"error": f"'{name}' not found in {v2}"}

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
    "name", "description", "text_content",
    "name_ja", "description_ja", "text_content_ja",
]


def search_literal(
    client: OpenSearch,
    pattern: str,
    fields: list[str] | None = None,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 200,
) -> dict:
    """Exact-phrase search across text fields.

    When patch_version is None (default): collapses by entity name and returns the
    latest version per entity; total reflects distinct entities, not raw index hits.
    When patch_version is specified: filters to that snapshot; total is the raw hit count.
    """
    search_fields = fields or _LITERAL_FIELDS
    filters = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})

    body: dict = {
        "size": limit,
        "track_total_hits": True,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": pattern,
                            "fields": search_fields,
                            "type": "phrase",
                        }
                    }
                ],
                "filter": filters,
            }
        },
    }

    if not patch_version:
        # Collapse by entity name, sorting by patch_version desc so the representative
        # doc is the latest version of each entity. Cardinality agg gives the exact
        # distinct-entity count (accurate for corpora well under precision_threshold).
        body["sort"] = [{"patch_version": "desc"}, "_score"]
        body["collapse"] = {"field": "name.keyword"}
        body["aggs"] = {
            "distinct_entities": {
                "cardinality": {"field": "name.keyword", "precision_threshold": 40000}
            }
        }

    resp = client.search(index=INDEX, body=body)

    if not patch_version:
        total = resp["aggregations"]["distinct_entities"]["value"]
    else:
        total = resp["hits"]["total"]["value"]

    return {"total": total, "results": [hit["_source"] for hit in resp["hits"]["hits"]]}


def list_entity_types(client: OpenSearch) -> list[str]:
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {"types": {"terms": {"field": "entity_type", "size": 50}}},
        },
    )
    return [b["key"] for b in resp["aggregations"]["types"]["buckets"]]
