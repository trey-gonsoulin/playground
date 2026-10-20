"""OpenSearch client for self-hosted EC2 OpenSearch with basic auth + private-CA TLS."""

from __future__ import annotations

import fnmatch
import functools
import json
import logging
import os
import re
import ssl
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, overload

import boto3
from opensearchpy import OpenSearch, Urllib3HttpConnection
from opensearchpy.exceptions import (
    AuthenticationException,
    SerializationError,
    TransportError,
)
from opensearchpy.exceptions import ConnectionError as SearchConnectionError

from elden_ring._calc import STATS, attack_rating, spell_damage
from elden_ring._field_notes import _FIELD_NOTES
from elden_ring._schema import (
    _QUEST_CONDITION,
    _QUEST_WHEN,
    _SET_WHEN,
    INDEX,
    INDEX_MAPPING,
    _disabled_paths,
    _mapping_paths,
)


# ---------------------------------------------------------------------------
# Module-level cache — survives across warm Lambda invocations.
# Cold starts re-derive endpoint + password via one API call each.
# ---------------------------------------------------------------------------
@dataclass
class _Cache:
    endpoint: str | None = None
    password: str | None = None
    client: OpenSearch | None = None


_cache = _Cache()


def _ec2() -> Any:  # a boto3 EC2 client (boto3.client is a factory, not a type)
    return boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _get_password() -> str:
    """Fetch the OpenSearch admin password from SSM SecureString (cached)."""
    if _cache.password:
        return _cache.password
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = ssm.get_parameter(
        Name=os.environ["OPENSEARCH_PASSWORD_SSM_PATH"],
        WithDecryption=True,
    )
    _cache.password = str(resp["Parameter"]["Value"])
    return _cache.password


def _get_current_endpoint() -> str:
    """Return the EC2 instance's current public DNS hostname.

    Raises RuntimeError if the instance is stopped (no public hostname assigned yet).
    """
    if _cache.endpoint:
        return _cache.endpoint

    resp = _ec2().describe_instances(InstanceIds=[os.environ["EC2_INSTANCE_ID"]])
    instance = resp["Reservations"][0]["Instances"][0]
    hostname = instance.get("PublicDnsName") or instance.get("PublicIpAddress")
    if not hostname:
        raise RuntimeError(
            "Search service is offline. Call start_search_service() first."
        )
    _cache.endpoint = hostname
    return hostname


# The node's private root CA (#437), pinned so the basic-auth hop over the
# internet is verified (#467). Public material, copied from
# /etc/opensearch/er-root-ca.pem; a rebuilt instance mints a new CA, so refresh
# this file then. The node cert's SANs (localhost, 127.0.0.1, elden-ring-search)
# don't include the changing EC2 DNS name, so the chain is checked but not the
# hostname.
CA_CERTS = str(Path(__file__).absolute().parent / "er-root-ca.pem")


def pinned_ssl_context() -> ssl.SSLContext:
    """Verify the chain against CA_CERTS only, without the hostname check.

    Python 3.13's strict X.509 mode stays on: the CA carries keyUsage (#473).
    """
    ctx = ssl.create_default_context(cafile=CA_CERTS)
    ctx.check_hostname = False
    return ctx


class PinnedCAConnection(Urllib3HttpConnection):
    """Urllib3HttpConnection that verifies the node cert against CA_CERTS (#467)."""

    def __init__(self, *args, **kwargs):
        kwargs["ssl_context"] = pinned_ssl_context()
        super().__init__(*args, **kwargs)

    def _create_urllib3_pool(self) -> None:
        super()._create_urllib3_pool()
        # opensearch-py drops ssl_assert_hostname when given an ssl_context, and
        # urllib3 then matches the hostname itself. The chain check still runs.
        self.pool.assert_hostname = False  # pyright: ignore[reportOptionalMemberAccess]


def tls_options() -> dict:
    """OpenSearch() kwargs for a TLS connection pinned to CA_CERTS (#467)."""
    return {"use_ssl": True, "connection_class": PinnedCAConnection}


def get_client() -> OpenSearch:
    """Return a cached OpenSearch client, creating one if needed."""
    if _cache.client is not None:
        return _cache.client

    host = _get_current_endpoint()
    _cache.client = OpenSearch(
        hosts=[{"host": host, "port": 9200}],
        http_auth=(os.environ["OPENSEARCH_USER"], _get_password()),
        **tls_options(),
        # Fail inside the HTTP API's 30 s cap so callers get UNREACHABLE (#456).
        # No retry: a dead host's connect timeout counts as a ConnectionError,
        # which opensearch-py retries, so one retry made it 2 x 20 s.
        timeout=20,
        max_retries=0,
    )
    return _cache.client


def reset_cache() -> None:
    """Forget the cached endpoint, password and client (#456)."""
    _cache.endpoint = _cache.password = _cache.client = None


UNREACHABLE = (
    "search service unreachable (stopped or restarted); "
    "call start_search_service() and retry"
)


def unreachable_as_error(fn):
    """Turn a dead or rotated search connection into {"error": UNREACHABLE} (#456).

    The instance has no Elastic IP, so a stop/start changes its hostname and a
    warm Lambda would keep calling the old one; a rotated SSM password would
    keep failing with 401. Either way the caches are dropped, so the next call
    (after start_search_service) re-reads the endpoint and password.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (SearchConnectionError, AuthenticationException) as e:
            # ConnectionTimeout and SSLError subclass SearchConnectionError.
            logging.getLogger(__name__).warning("search unreachable: %r", e)
            reset_cache()
            return {"error": UNREACHABLE}

    return wrapper


def _instance_state(ec2: Any, instance_id: str) -> tuple[str, str | None]:
    """Return the instance's state name and public hostname (None if it has none)."""
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    hostname = inst.get("PublicDnsName") or inst.get("PublicIpAddress") or None
    return inst["State"]["Name"], hostname


def start_instance(budget_seconds: float = 20) -> dict:
    """Start the EC2 instance and poll OpenSearch for at most budget_seconds.

    Returns {"status": "ready"} once the cluster is green or yellow, otherwise
    {"status": "starting", "state": ..., "message": ...}. A cold start takes 2-3
    minutes, longer than the HTTP API's 30 s cap, so callers poll by calling
    again (#440). An instance that is still stopping is waited out first,
    because StartInstances rejects it with IncorrectInstanceState. Boto
    ClientErrors propagate to the tool.
    """
    instance_id = os.environ["EC2_INSTANCE_ID"]
    ec2 = _ec2()
    deadline = time.monotonic() + budget_seconds

    def starting(state: str, why: str) -> dict:
        message = f"{why}; call start_search_service() again in ~30 s"
        return {"status": "starting", "state": state, "message": message}

    state, hostname = _instance_state(ec2, instance_id)
    while state == "stopping" and time.monotonic() + 3 <= deadline:
        time.sleep(3)
        state, hostname = _instance_state(ec2, instance_id)
    if state == "stopping":
        return starting(state, "the instance is still stopping")
    if state in ("shutting-down", "terminated"):
        return {"status": "error", "state": state, "message": f"instance is {state}"}
    if state == "stopped":
        reset_cache()  # the old hostname went away with the stop (#456)
        ec2.start_instances(InstanceIds=[instance_id])

    # Wait for 'running' and a public hostname.
    while not (state == "running" and hostname):
        if time.monotonic() + 3 > deadline:
            return starting(state, "the instance is booting")
        time.sleep(3)
        state, hostname = _instance_state(ec2, instance_id)

    if hostname != _cache.endpoint:
        reset_cache()  # drop a client bound to an old hostname
        _cache.endpoint = hostname

    # Poll OpenSearch health (verified TLS, #467) until green/yellow.
    while True:
        remaining = deadline - time.monotonic()
        try:
            # The transport pops request_timeout from params (the typed signature
            # has no request_timeout keyword; the query_params decorator moves it
            # into params unchanged).
            health = get_client().cluster.health(
                params={"request_timeout": max(1.0, min(5.0, remaining))}
            )
            if health.get("status") in ("green", "yellow"):
                return {"status": "ready"}
        except (TransportError, SerializationError):
            # TransportError covers refused connections, timeouts (ReadTimeout)
            # and the 503 while the security index loads.
            pass
        if time.monotonic() + 3 > deadline:
            return starting("running", "OpenSearch is still initialising")
        time.sleep(3)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def analyze_text(client: OpenSearch, text: str) -> dict:
    """Return token streams for text under all four indexed JP analyzers.

    Calls OpenSearch's _analyze API via the field path so the result reflects
    exactly what each search mode applies:
    - standard: CJK unigram (description_ja), used by search_literal() default
    - kuromoji_analyzer: search-mode segmentation + baseform, POS/stopword
      filtering and stemming (description_ja.ja), used by search()
    - kuromoji_segmenter: morpheme segmentation (description_ja.morph), use_kuromoji=True
    - kuromoji_lemmatizer: segmentation + baseform (description_ja.lemma), use_lemmatize=True

    Returns {"standard": [...], "kuromoji_analyzer": [...],
    "kuromoji_segmenter": [...], "kuromoji_lemmatizer": [...]}
    """

    def _tokens(field: str) -> list[str]:
        resp = client.indices.analyze(index=INDEX, body={"field": field, "text": text})
        return [t["token"] for t in resp["tokens"]]

    return {
        "standard": _tokens("description_ja"),
        "kuromoji_analyzer": _tokens("description_ja.ja"),
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


def _dlc_filter(is_dlc: bool | None) -> list[dict]:
    """Filter clause keeping only DLC (True) or only base-game (False) docs (#297)."""
    return [] if is_dlc is None else [{"term": {"is_dlc": is_dlc}}]


def _variant_filter(collapse_variants: bool) -> list[dict]:
    """Filter clause dropping item variant docs, keeping each family's base (#111).

    Weapon affinities (Heavy Dagger), talisman ranks (Erdtree's Favor +2), flask +N
    and altered armor are their own docs naming their base in ``base_item``;
    collapsing keeps the docs without it (bases, and everything outside a family).
    Supersedes the weapon-only collapse_affinity (#21; alias removed in #458).
    """
    if not collapse_variants:
        return []
    return [{"bool": {"must_not": [{"exists": {"field": "base_item"}}]}}]


def _base_filters(
    *,
    entity_type: str | None,
    patch_version: str | None,
    source: str | None,
    include_unavailable: bool,
    collapse_variants: bool,
    is_dlc: bool | None,
) -> list[dict]:
    """The filter clauses search() and search_literal() share (#458)."""
    filters: list[dict] = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    if patch_version:
        filters.append({"term": {"patch_version": patch_version}})
    if source:
        filters.append({"term": {"source": source}})
    filters += _availability_filter(include_unavailable)
    filters += _variant_filter(collapse_variants)
    filters += _dlc_filter(is_dlc)
    return filters


# Source values from the retired erdb/fextralife layers (2026-09-21). An unknown
# filter value matches nothing, so name these specifically rather than returning 0.
_RETIRED_SOURCES = frozenset({"erdb", "fextralife"})


def unmapped_paths(docs: list[dict]) -> list[str]:
    """Dotted paths in ``docs`` that the strict mapping would reject (#448).

    Walks dicts and lists of dicts, skipping None values and ``_INTERNAL_FIELDS``;
    a mapped object without ``properties`` (``enabled: false``) is not descended.
    Lets a loader refuse a build before connecting instead of failing per doc
    with ``strict_dynamic_mapping_exception`` part-way through a reindex.
    """
    props = INDEX_MAPPING["mappings"]["properties"]
    out: set[str] = set()

    def walk(value, props: dict, prefix: str) -> None:
        if isinstance(value, list):
            for v in value:
                walk(v, props, prefix)
            return
        if not isinstance(value, dict):
            return
        for key, v in value.items():
            if v is None or (not prefix and key in _INTERNAL_FIELDS):
                continue
            path = prefix + key
            spec = props.get(key)
            if spec is None:
                out.add(path)
            elif "properties" in spec:
                walk(v, spec["properties"], path + ".")

    for d in docs:
        walk(d, props, "")
    return sorted(out)


@functools.cache
def _queryable_paths() -> frozenset[str]:
    """Mapped paths plus their subfields (description_ja.lemma, name.keyword)."""
    paths = set()
    for path, spec in _mapping_paths(INDEX_MAPPING["mappings"]["properties"]).items():
        paths.add(path)
        paths.update(f"{path}.{sub}" for sub in spec.get("fields", {}))
    return frozenset(paths)


def _unsearchable_fields(names: list[str] | None) -> list[str]:
    """Names that are, or sit inside, an enabled: false object (#446).

    Such a path is returned by get_entity / include_fields and diffed by
    text_changed_between, but a query on it matches nothing.
    """
    disabled = _disabled_paths()
    return [
        n
        for n in names or []
        if n in disabled or any(n.startswith(d + ".") for d in disabled)
    ]


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


# One cap for every entity_type / source terms aggregation (#463). The 1.17 data
# has ~40 entity types and 24-27 sources; a terms agg silently drops buckets past
# its size, so _agg_truncation_warnings flags any agg that hit the cap.
_CATALOG_AGG_SIZE = 100


def _agg_truncation_warnings(aggs: dict) -> list[str]:
    """One warning per terms aggregation that left buckets out, logged as well.

    A terms agg counts the docs of the buckets past its size in
    sum_other_doc_count, so a non-zero count means the listing is incomplete.
    """
    warnings = [
        f"{name}: more values than the {_CATALOG_AGG_SIZE}-bucket cap; "
        f"{agg['sum_other_doc_count']} docs are in unlisted buckets"
        for name, agg in aggs.items()
        if isinstance(agg, dict) and agg.get("sum_other_doc_count", 0) > 0
    ]
    for w in warnings:
        logging.getLogger(__name__).warning("terms agg truncated: %s", w)
    return warnings


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
                "entity_type": {
                    "terms": {"field": "entity_type", "size": _CATALOG_AGG_SIZE}
                },
                "source": {"terms": {"field": "source", "size": _CATALOG_AGG_SIZE}},
                "patch_version": {"terms": {"field": "patch_version", "size": 100}},
            },
        },
    )
    aggs = resp["aggregations"]
    _agg_truncation_warnings(aggs)

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
    # An enabled: false object has no exists entry, so it would always look
    # uncarried; search_literal rejects it up front (#446).
    unsearchable = set(_unsearchable_fields(bases))
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
        for f, base in zip(fields, bases, strict=True)
        if not counts.get(base) and base not in unsearchable
    ]


def search(
    client: OpenSearch,
    query: str,
    *,
    entity_type: str | None = None,
    patch_version: str | None = None,
    limit: int = 20,
    include_fields: list[str] | None = None,
    count_only: bool = False,
    source: str | None = None,
    include_unavailable: bool = False,
    collapse_variants: bool = False,
    is_dlc: bool | None = None,
) -> dict:
    """Relevance search: {"results": [doc, ...]}, {"total": N} or {"error": ...}.

    Always a dict, never a bare list: FastMCP sends a list as one block per
    element, and an empty one as no content at all (#441, #478).
    """
    if err := _precheck(source, include_fields):
        return err
    patch_version = _request_version(client, patch_version)
    filters = _base_filters(
        entity_type=entity_type,
        patch_version=patch_version,
        source=source,
        include_unavailable=include_unavailable,
        collapse_variants=collapse_variants,
        is_dlc=is_dlc,
    )

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
                        "field": "entity_key",
                        "precision_threshold": 40000,
                    }
                }
            }
    else:
        # Without a specific patch filter, collapse by entity (entity_type::name,
        # #443) to deduplicate across the indexed patch versions. Sort by _score
        # first so groups stay in relevance order (the primary contract of this
        # tool). A group's top hit can be an older patch whose rewritten text scores
        # higher, or the only patch whose text matches at all, so _swap_newest
        # replaces it with the entity's newest patch under the same filters, keeping
        # the group's score (#442).
        if not patch_version:
            body["sort"] = [{"_score": "desc"}, {"patch_version": "desc"}]
            body["collapse"] = {"field": "entity_key"}

    resp = client.search(index=INDEX, body=body)

    if count_only:
        if not patch_version:
            out: dict = {"total": resp["aggregations"]["distinct_entities"]["value"]}
        else:
            out = {"total": resp["hits"]["total"]["value"]}
    else:
        hits = resp["hits"]["hits"]
        if not patch_version:
            hits = _swap_newest(client, hits, filters, include_fields)
        out = {
            "results": [
                {"score": hit["_score"], **hit.get("_source", {})} for hit in hits
            ]
        }
    if out in ({"results": []}, {"total": 0}):
        return _check_filter_values(client, entity_type, source, patch_version) or out
    return out


def _swap_newest(
    client: OpenSearch,
    hits: list[dict],
    filters: list[dict],
    include_fields: list[str] | None,
) -> list[dict]:
    """Replace each collapsed hit's doc with its entity's newest patch (#442).

    One query, no text clause: the same filters plus the hits' entity_keys, sorted
    newest first and collapsed on entity_key. The newest patch is returned even when
    only an older patch's text matched. A key it doesn't return keeps its matched doc.
    """
    keys = [k for h in hits if (k := _hit_entity_key(h))]
    if not keys:
        return hits
    body: dict = {
        "size": len(keys),
        "query": {"bool": {"filter": [*filters, {"terms": {"entity_key": keys}}]}},
        "sort": [{"patch_version": "desc"}],
        "collapse": {"field": "entity_key"},
    }
    if include_fields is not None:
        body["_source"] = include_fields
    resp = client.search(index=INDEX, body=body)
    newest = {
        k: h.get("_source", {})
        for h in resp["hits"]["hits"]
        if (k := _hit_entity_key(h))
    }
    return [
        {**h, "_source": newest.get(_hit_entity_key(h) or "", h.get("_source", {}))}
        for h in hits
    ]


def _hit_entity_key(hit: dict) -> str | None:
    """A collapsed hit's entity_key (the collapse puts it in hit["fields"])."""
    vals = hit.get("fields", {}).get("entity_key")
    return vals[0] if vals else hit.get("_source", {}).get("entity_key")


def _ascii_fold(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# Types whose docs reuse another doc's exact name (a keepsake doc is named after the
# item it grants, #475). An untyped name lookup ranks them last, so
# get_entity("Golden Seed") stays the upgrade material.
_NAME_LOOKUP_DEMOTED_TYPES = ("keepsake",)


def _newest_hit(
    client: OpenSearch, term: dict, entity_type: str | None
) -> tuple[dict | None, list[str]]:
    """Newest doc matching ``term``, plus (untyped only) every entity_type that
    carries it, from the same query (#468)."""
    filters: list[dict] = [{"term": term}]
    sort: list[dict] = []
    if entity_type:
        filters.append({"term": {"entity_type": entity_type}})
    else:
        sort.append(
            {
                "_script": {
                    "type": "number",
                    "order": "asc",
                    "script": {
                        "source": "params.demoted.contains(doc['entity_type'].value) ? 1 : 0",
                        "params": {"demoted": list(_NAME_LOOKUP_DEMOTED_TYPES)},
                    },
                }
            }
        )
    # entity_type breaks ties between same-name docs of two types (#443), so an
    # untyped shared name resolves the same way every time (#468).
    sort += [{"patch_version": "desc"}, {"entity_type": "asc"}]
    body: dict = {"size": 1, "query": {"bool": {"filter": filters}}, "sort": sort}
    if not entity_type:
        body["aggs"] = {
            "types": {"terms": {"field": "entity_type", "size": _CATALOG_AGG_SIZE}}
        }
    resp = client.search(index=INDEX, body=body)
    hits = resp["hits"]["hits"]
    buckets = resp.get("aggregations", {}).get("types", {}).get("buckets", [])
    return (hits[0]["_source"] if hits else None), sorted(b["key"] for b in buckets)


def _newest_doc(client: OpenSearch, term: dict, entity_type: str | None) -> dict | None:
    return _newest_hit(client, term, entity_type)[0]


def _resolve_entity_doc(
    client: OpenSearch, name: str, entity_type: str | None = None
) -> tuple[dict, bool, list[str]] | None:
    """Newest doc matching an input name, whether the match was historical, and
    (untyped only) the other entity_types carrying that name (#468).

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
        doc, types = _newest_hit(client, term, entity_type)
        if doc:
            return doc, historical, [t for t in types if t != doc["entity_type"]]
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


@overload
def trim_placements(doc: dict, limit: int = ...) -> dict: ...
@overload
def trim_placements(doc: None, limit: int = ...) -> None: ...
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
    *,
    entity_type: str | None = None,
    include_variants: bool = False,
    patch_version: str | None = None,
) -> dict | None:
    requested_version = patch_version
    if patch_version:
        labels, err = _loaded_versions(client, patch_version)
        if err:
            return err
        patch_version = labels[patch_version]
    resolved = _resolve_entity_doc(client, name, entity_type)
    if resolved is None:
        return _check_filter_values(client, entity_type)
    doc, historical, other_types = resolved
    if patch_version:
        # As-of the entity_type's own version set (#206): dialogue is loaded once
        # per Data0 group, so a patch between representatives maps to the earlier one.
        etype = doc["entity_type"]
        resolved_version, err = _asof_version(
            patch_version, _entity_versions(client, etype), etype
        )
        if resolved_version is None:
            return err
        found = _version_doc(client, doc["name"], doc["entity_type"], resolved_version)
        if not found:
            return {"error": f"'{doc['name']}' not present in {resolved_version}"}
        out = _public(found)
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
    if other_types:
        out["other_entity_types"] = other_types
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


def _canonical_version(version: str, loaded: list[str]) -> str:
    """The loaded patch label a requested version means, ignoring trailing zeros.

    Labels 1.02–1.06 have no third part while 1.07.0 on do, so "1.02.0" and
    "1.10" would otherwise be "not loaded". Returns the input unchanged when it's
    loaded already or doesn't match exactly one loaded label. Pure: ``loaded`` is
    the global version list, fetched once per call by the caller (#458).
    """
    if version in loaded:
        return version
    matches = [v for v in loaded if _trimmed_key(v) == _trimmed_key(version)]
    return matches[0] if len(matches) == 1 else version


def _request_version(client: OpenSearch, version: str | None) -> str | None:
    """A search filter's patch_version as its loaded label (one aggregation, and
    none without a version)."""
    if not version:
        return version
    return _canonical_version(version, _entity_versions(client, None))


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


def _loaded_versions(
    client: OpenSearch, *versions: str
) -> tuple[dict[str, str], dict | None]:
    """Each requested version's loaded label, plus an error for the first that no
    type has.

    Shared by get_entity, diff_entities, text_changed_between and
    calculate_attack_rating (#439): a request must be a real patch somewhere in the
    index before it's resolved as-of one type's (possibly sparser) set. One global
    version aggregation however many versions (#458).
    """
    loaded = _entity_versions(client, None)
    requested: dict[str, str] = {}
    for v in versions:
        requested[v] = canonical = _canonical_version(v, loaded)
        if canonical not in loaded:
            return requested, {
                "error": f"patch version '{canonical}' is not loaded; "
                f"loaded versions: {loaded}"
            }
    return requested, None


def _asof_version(
    version: str, versions: list[str], entity_type: str
) -> tuple[str | None, dict | None]:
    """A loaded version resolved as-of one entity_type's own ``versions`` (#206,
    #439), or None and an error."""
    if not versions:
        return None, {"error": f"no '{entity_type}' documents are loaded"}
    resolved = _resolve_asof(version, versions)
    if resolved is None:
        return None, {
            "error": f"no {entity_type} data at or before '{version}' "
            f"(earliest loaded: {versions[0]})"
        }
    return resolved, None


def _resolve_pair(
    client: OpenSearch, labels: list[str], entity_type: str
) -> tuple[dict[str, str], dict | None]:
    """Loaded labels resolved as-of entity_type's versions ({label: snapshot}),
    from one aggregation, or the first label's error (#439, #458)."""
    versions = _entity_versions(client, entity_type)
    resolved: dict[str, str] = {}
    for v in labels:
        res, err = _asof_version(v, versions, entity_type)
        if res is None:
            return resolved, err or {}
        resolved[v] = res
    return resolved, None


def _version_doc(
    client: OpenSearch, name: str, entity_type: str, version: str
) -> dict | None:
    """The one doc for a canonical name, type and loaded version."""
    resp = client.search(
        index=INDEX,
        body={
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"name.keyword": name}},
                        {"term": {"entity_type": entity_type}},
                        {"term": {"patch_version": version}},
                    ]
                }
            },
        },
    )
    hits = resp["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def _resolution_info(
    entity_type: str, requested: dict[str, str], resolved: dict[str, str]
) -> dict:
    """requested_patch_versions / resolved_patch_versions / note for a two-version
    tool (#439). ``requested`` maps each input to its loaded label, ``resolved``
    each loaded label to the snapshot that answered it."""
    out: dict = {}
    if remapped := {k: v for k, v in requested.items() if k != v}:
        out["requested_patch_versions"] = remapped
    if moved := {k: v for k, v in resolved.items() if k != v}:
        out["resolved_patch_versions"] = moved
    snaps = set(resolved.values())
    if len(resolved) > 1 and len(snaps) == 1:
        (snap,) = snaps
        out["note"] = (
            f"{' and '.join(resolved)} both resolve to the one {entity_type} "
            f"snapshot {snap}, so nothing can differ"
        )
    return out


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
    *,
    entity_type: str | None = None,
) -> dict:
    requested, err = _loaded_versions(client, v1, v2)
    if err:
        return err
    v1, v2 = requested[v1], requested[v2]

    # Accept historical/diacritic-folded names; diff the canonical entity (#59), of
    # the type the name resolved to so both sides come from one type and its own
    # (possibly sparser) version set (#439).
    found = _resolve_entity_doc(client, name, entity_type)
    if found is None:
        return {"error": f"'{name}' not found in any loaded version"}
    queried_name, name = name, found[0]["name"]
    etype = entity_type or found[0]["entity_type"]

    resolved, err = _resolve_pair(client, [v1, v2], etype)
    if err is not None:
        return err
    r1, r2 = resolved[v1], resolved[v2]

    doc1 = _version_doc(client, name, etype, r1)
    doc2 = _version_doc(client, name, etype, r2)
    if not doc1 or not doc2:
        # Report the resolved version so the message names where we actually looked.
        missing = [v for v, d in ((r1, doc1), (r2, doc2)) if not d]
        return {"error": f"'{name}' not present in {' and '.join(missing)}"}

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
        "entity_type": etype,
        "changed": bool(changed),
        "changed_fields": changed,
        "unchanged_fields": unchanged,
    }
    if queried_name != name:
        result["queried_name"] = queried_name
    if found[2]:
        result["other_entity_types"] = found[2]
    return result | _resolution_info(etype, requested, resolved)


# Per-pattern page size for the patterns-OR union path (the 10k max_result_window).
# The union pass fetches only the key fields, so a full page is ~1 MB, not ~48 MB
# of full docs (#447). One patch has ~20k distinct names, so a broad pattern can
# match more than a page: the union total is then a lower bound
# (total_is_lower_bound).
_UNION_FETCH_SIZE = 10000

# Most distinct patterns one patterns-OR call may run (one sub-query each, #447).
_MAX_PATTERNS = 20

# The union pass's sub-query source: the dedupe key plus the patch, which with
# them makes the doc's bulk _id (opensearch_load._to_bulk_actions) for the full fetch.
_UNION_KEY_FIELDS = ["name", "entity_type", "patch_version"]

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
# Japanese text fields whose analyzer lives on a subfield, not the base field:
# every mapped path with a .morph subfield, so a new *_ja field routes without a
# hand-kept list (subtitles_ja was missing, #446). subtitles / subtitles_ja stay
# out of the default _LITERAL_FIELDS*: cutscene text is opt-in via fields=.
_JP_BASE_FIELDS = frozenset(
    path
    for path, spec in _mapping_paths(INDEX_MAPPING["mappings"]["properties"]).items()
    if "morph" in spec.get("fields", {})
)
# Same as _LITERAL_FIELDS with the Japanese fields routed through the
# segmentation-only .morph subfield (phrase queries respect kuromoji morpheme
# boundaries) or the lemmatizing .lemma subfield (kuromoji_baseform only, so a
# baseform query matches all inflections). Derived, not hand-kept (#458).
_LITERAL_FIELDS_MORPH = [
    f"{f}.morph" if f in _JP_BASE_FIELDS else f for f in _LITERAL_FIELDS
]
_LITERAL_FIELDS_LEMMA = [
    f"{f}.lemma" if f in _JP_BASE_FIELDS else f for f in _LITERAL_FIELDS
]
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


# Item regions / locations hold pickup placements only; the dropping enemies'
# placements are in drop_regions / drop_locations (#141). A literal search over
# the pickup field also searches its drop twin by default (#344), so
# fields=["regions"] finds Black Knife where the Black Knife Assassin stands
# (drop_regions = Altus Plateau). locations also brings drop_near_locations
# (#347/#352): dungeons or landmarks a scripted dropping enemy stands just outside.
_DROP_FIELD_TWINS = {
    "regions": ("drop_regions",),
    "locations": ("drop_locations", "drop_near_locations"),
}


def _with_drop_fields(fields: list[str]) -> list[str]:
    """Add drop_regions / drop_locations (+ drop_near_locations) after regions /
    locations when missing."""
    out = list(fields)
    for f in fields:
        at = out.index(f) + 1
        for twin in _DROP_FIELD_TWINS.get(f, ()):
            if twin not in out:
                out.insert(at, twin)
                at += 1
    return out


def search_literal(
    client: OpenSearch,
    pattern: str | None = None,
    *,
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
    collapse_variants: bool = False,
    is_dlc: bool | None = None,
    include_drop_fields: bool = True,
) -> dict:
    """Exact-phrase search across text fields, with optional structural filters.

    When patch_version is None (default): collapses by entity_key (entity_type::name,
    #443) and returns each entity's newest matching patch; total reflects distinct
    entities, not raw index hits.
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
    (name_ja, description_ja, text_content_ja, effect_text_ja, info_text_ja,
    subtitles_ja) are routed
    to the matching .lemma/.morph
    subfield automatically, so fields=["description_ja"] with use_lemmatize=True searches
    description_ja.lemma. Without this, an explicit field would search the surface form and
    silently defeat the analyzer.

    Unknown values return {"error": ...} instead of a silent 0 (#205): a retired
    source or unmapped field name up front, and an entity_type / source /
    patch_version that no document carries when the result is empty. An empty
    result over a mapped field that no doc of the type carries adds "warnings".

    fields naming regions / locations also search drop_regions / drop_locations
    (and drop_near_locations, #347) unless include_drop_fields=False (#344), so an
    item dropped where its enemy dies is found by the place as well as a pickup
    there.

    A fields= entry that is an enabled: false object (placements, shop_listings,
    upgrade_curve, set_when, ...; describe_index marks it searchable: false) is
    an error: it is stored, not indexed, so a query on it matches nothing (#446).
    """
    if unsearchable := _unsearchable_fields(fields):
        return {
            "error": f"field(s) {unsearchable} are stored, not indexed; read it "
            "with get_entity"
        }
    if err := _precheck(source, fields, include_fields):
        return err
    if sort_id_mod is not None and not (
        sort_id_mod >= 1 and 0 <= sort_id_remainder < sort_id_mod
    ):
        return {
            "error": f"sort_id_mod={sort_id_mod}, sort_id_remainder="
            f"{sort_id_remainder}: need sort_id_mod >= 1 and "
            "0 <= sort_id_remainder < sort_id_mod"
        }
    if patterns and (n := len({p for p in [pattern, *patterns] if p})) > _MAX_PATTERNS:
        return {
            "error": f"patterns: {n} distinct patterns, at most {_MAX_PATTERNS} per "
            "call (one sub-query each); split the search"
        }
    search_fields = (
        _with_drop_fields(fields) if fields and include_drop_fields else fields
    )
    requested_version = patch_version
    patch_version = _request_version(client, patch_version)
    out = _search_literal(
        client,
        pattern=pattern,
        fields=search_fields,
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
        collapse_variants=collapse_variants,
        is_dlc=is_dlc,
    )
    if not out["total"]:
        if err := _check_filter_values(client, entity_type, source, patch_version):
            return err
        # Warn only about the fields the caller named, not the drop twins.
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
    *,
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
    collapse_variants: bool = False,
    is_dlc: bool | None = None,
) -> dict:
    if fields is not None:
        search_fields = _route_literal_fields(fields, use_lemmatize, use_kuromoji)
    elif use_lemmatize:
        search_fields = _LITERAL_FIELDS_LEMMA
    elif use_kuromoji:
        search_fields = _LITERAL_FIELDS_MORPH
    else:
        search_fields = _LITERAL_FIELDS
    filters = _base_filters(
        entity_type=entity_type,
        patch_version=patch_version,
        source=source,
        include_unavailable=include_unavailable,
        collapse_variants=collapse_variants,
        is_dlc=is_dlc,
    )
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
        all_patterns = list(dict.fromkeys(all_patterns))  # dedupe, keep order (#447)

    if len(all_patterns) > 1 or patterns:
        # OpenSearch absorbs phrase clauses that share CJK tokens in a bool.should,
        # silently returning fewer results than any individual pattern alone (#64).
        # The only safe OR is one query per pattern with a Python-side union.
        #
        # The union pass fetches a large per-pattern page of key fields only and
        # dedupes on (entity_type, name) (#443), so total is exact unless a pattern
        # matched more than a page (then total_is_lower_bound). Full docs are then
        # fetched for just the first `limit` keys (#447).
        seen: dict[tuple[str, str], dict] = {}
        lower_bound = False
        for p in all_patterns:
            r = _search_literal(
                client,
                pattern=p,
                fields=fields,
                entity_type=entity_type,
                patch_version=patch_version,
                limit=_UNION_FETCH_SIZE,
                include_fields=_UNION_KEY_FIELDS,
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
                collapse_variants=collapse_variants,
                is_dlc=is_dlc,
            )
            docs = r.get("results", [])
            lower_bound |= r["total"] > len(docs)
            for doc in docs:
                seen.setdefault((doc.get("entity_type", ""), doc.get("name", "")), doc)
        out: dict = {"total": len(seen)}
        if lower_bound:
            out["total_is_lower_bound"] = True
        if count_only:
            return out
        keys = list(seen.values())[:limit]
        return out | {"results": _union_docs(client, keys, include_fields)}

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
        # Cardinality agg gives exact distinct-entity count (per entity_type::name,
        # #443). The patch_version-first sort makes each collapse group's top hit its
        # newest matching patch, so no inner_hits (they cost one search per group).
        body["aggs"] = {
            "distinct_entities": {
                "cardinality": {"field": "entity_key", "precision_threshold": 40000}
            }
        }
        if not count_only:
            body["sort"] = [{"patch_version": "desc"}, "_score"]
            body["collapse"] = {"field": "entity_key"}

    resp = client.search(index=INDEX, body=body)

    if not patch_version:
        total = resp["aggregations"]["distinct_entities"]["value"]
    else:
        total = resp["hits"]["total"]["value"]

    if count_only:
        return {"total": total}
    return {
        "total": total,
        "results": [h.get("_source", {}) for h in resp["hits"]["hits"]],
    }


def _union_docs(
    client: OpenSearch, keys: list[dict], include_fields: list[str] | None
) -> list[dict]:
    """The full docs behind the patterns union's key docs, in union order: one ids
    query on their bulk _ids (entity_type::name::patch_version, #447)."""
    if not keys:
        return []
    ids = [
        f"{d.get('entity_type')}::{d.get('name')}::{d.get('patch_version', 'unknown')}"
        for d in keys
    ]
    body: dict = {"size": len(ids), "query": {"ids": {"values": ids}}}
    if include_fields is not None:
        body["_source"] = include_fields
    resp = client.search(index=INDEX, body=body)
    by_id = {h["_id"]: h.get("_source", {}) for h in resp["hits"]["hits"]}
    # A doc gone since the union pass (a concurrent load) keeps its key fields.
    return [
        by_id.get(i)
        or {k: v for k, v in d.items() if include_fields is None or k in include_fields}
        for i, d in zip(ids, keys, strict=True)
    ]


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
    *,
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
    requested_version = patch_version
    if patch_version:
        # An unloaded version ("1.99") is an error, not the newest patch (#439).
        labels, err = _loaded_versions(client, patch_version)
        if err:
            return err
        version, err = _asof_version(
            labels[patch_version], _entity_versions(client, entity_type), entity_type
        )
        if version is None:
            return err or {}
    else:
        version = _entity_versions(client, entity_type)[-1]
    doc = _version_doc(client, canonical, entity_type, version)
    if not doc:
        return {"error": f"'{canonical}' not present in {version}"}
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
    if requested_version and requested_version != version:
        result["requested_patch_version"] = requested_version
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
    doc = _version_doc(client, name, "spell", spell_version) if spell_version else None
    if not doc:
        return {"error": f"spell '{name}' not present at or before {version}"}
    entry = (doc.get("spell_attacks") or [{}])[0]
    base = entry.get("attack_power")
    if not base:
        return {
            "error": f"'{name}' deals no damage (a buff, heal or status-only spell)"
        }
    if entry.get("flat"):
        # a flat contact hit no catalyst scales (Mantle of Thorns' thorns, #331)
        scaling = {}
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
    casts = _CASTS.get(catalyst.get("menu_category") or "")
    if casts and doc.get("menu_category") and doc["menu_category"] != casts:
        out["notes"] = [
            f"a {catalyst['menu_category']} can't cast {doc['menu_category'].lower()}s"
        ]
    if entry.get("flat"):
        out.setdefault("notes", []).append(
            "a flat contact hit (the caster's rolls): the catalyst doesn't scale it"
        )
    return out


def _leaf_field_error(field: str) -> dict | None:
    """Error for a text_changed_between field with no stored value (#438).

    A search subfield (name.keyword, description_ja.lemma) or a wildcard would read
    as null at both versions and report "nothing changed". A group (stats) or list
    (phases) compares as one whole value; paths through a list of objects are caught
    per document in _source.
    """
    if "*" in field:
        return {"error": f"'{field}': name one field, wildcards aren't supported here"}
    if field not in _mapping_paths(INDEX_MAPPING["mappings"]["properties"]):
        parent = field.rsplit(".", 1)[0]
        return {
            "error": f"'{field}' is a search subfield of '{parent}' with no stored "
            f"value of its own; compare '{parent}' instead"
        }
    return None


def _source_value(source: dict, field: str) -> tuple[object, str | None]:
    """A dotted path's value in _source, plus the list prefix it ran through (if any)."""
    value: object = source
    parts = field.split(".")
    for i, part in enumerate(parts):
        if isinstance(value, list):
            return None, ".".join(parts[:i])
        if not isinstance(value, dict):
            return None, None
        value = value.get(part)
    return value, None


class _ListPathError(Exception):
    """A text_changed_between path ran through a list of objects (#438)."""


# text_changed_between pages through each version's docs this many at a time
# (search_after on name.keyword), so no type is silently cut at the 10k window (#438).
_CHANGED_PAGE_SIZE = 5000


def text_changed_between(
    client: OpenSearch,
    entity_type: str,
    field: str,
    v1: str,
    v2: str,
    *,
    count_only: bool = False,
) -> dict:
    """Return all entities of entity_type where field differs between v1 and v2.

    Pages through every entity for each version; comparison happens in Python.
    Only entities present in both versions are included (added/removed entities
    are excluded — use diff_entities for per-entity existence checks).
    """
    if err := _precheck(None, [field]) or _leaf_field_error(field):
        return err
    # A requested version must be a real loaded patch somewhere in the index …
    requested, err = _loaded_versions(client, v1, v2)
    if err:
        return err
    v1, v2 = requested[v1], requested[v2]

    # … but this entity_type may be indexed at a sparser set of versions (dialogue
    # is stored once per Data0 group), so resolve each request as-of that set.
    resolved, err = _resolve_pair(client, [v1, v2], entity_type)
    if err is not None:
        return err
    r1, r2 = resolved[v1], resolved[v2]

    def _fetch_all(version: str) -> dict[str, object]:
        values: dict[str, object] = {}
        after = None
        while True:
            body: dict = {
                "size": _CHANGED_PAGE_SIZE,
                "_source": ["name", field],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"entity_type": entity_type}},
                            {"term": {"patch_version": version}},
                        ]
                    }
                },
                "sort": [{"name.keyword": "asc"}],
            }
            if after is not None:
                body["search_after"] = after
            hits = client.search(index=INDEX, body=body)["hits"]["hits"]
            for hit in hits:
                value, through = _source_value(hit["_source"], field)
                if through:
                    raise _ListPathError(through)
                values[hit["_source"]["name"]] = value
            if len(hits) < _CHANGED_PAGE_SIZE:
                return values
            after = hits[-1]["sort"]

    try:
        docs_v1 = _fetch_all(r1)
        docs_v2 = _fetch_all(r2)
    except _ListPathError as e:
        return {
            "error": f"'{field}' runs through '{e}', a list of objects, so an entity "
            f"has no single value for it; compare the whole list (field='{e}'), or "
            "use diff_entities(name, v1, v2) for one entity"
        }

    results = []
    for name in sorted(set(docs_v1) & set(docs_v2)):
        val1 = docs_v1[name]
        val2 = docs_v2[name]
        if val1 != val2:
            results.append({"name": name, "text_before": val1, "text_after": val2})

    info = _resolution_info(entity_type, requested, resolved)
    if count_only:
        return {"total": len(results)} | info
    return {"total": len(results), "results": results} | info


_ENTITY_COUNT_AGG = {
    "entity_count": {
        "cardinality": {"field": "name.keyword", "precision_threshold": 1000}
    }
}


def list_menu_categories(
    client: OpenSearch,
    entity_type: str | None = None,
    source: str | None = None,
    *,
    include_unavailable: bool = False,
    collapse_variants: bool = True,
) -> dict[str, int] | dict[str, dict[str, int]]:
    """Return distinct menu_category values with distinct entity counts.

    With entity_type: returns {category: entity_count} sorted by category name.
    Without entity_type: returns {entity_type: {category: entity_count}} for
    every type that has at least one doc with menu_category set.

    Counts are distinct-entity counts (cardinality on name.keyword), not raw
    doc counts, so multi-patch duplication doesn't inflate the numbers. As in
    the search tools, cut / unobtainable docs are left out unless
    include_unavailable, and variant docs (weapon affinities, talisman ranks,
    altered armor) unless collapse_variants=False, so the counts match
    search(count_only=True, collapse_variants=True) (#465).

    An unknown or retired entity_type / source returns {"error": ...} (#205).
    """
    if err := _precheck(source):
        return err
    out = _menu_categories(
        client,
        entity_type,
        source,
        include_unavailable=include_unavailable,
        collapse_variants=collapse_variants,
    )
    if not out:
        return _check_filter_values(client, entity_type, source) or out
    return out


def _menu_categories(
    client: OpenSearch,
    entity_type: str | None = None,
    source: str | None = None,
    *,
    include_unavailable: bool = False,
    collapse_variants: bool = True,
) -> dict[str, int] | dict[str, dict[str, int]]:
    shared = _availability_filter(include_unavailable) + _variant_filter(
        collapse_variants
    )
    if entity_type:
        filters: list[dict] = [{"term": {"entity_type": entity_type}}]
        if source:
            filters.append({"term": {"source": source}})
        filters += shared
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
    base_filters += shared
    body = {
        "size": 0,
        "query": {"bool": {"filter": base_filters}},
        "aggs": {
            "by_type": {
                "terms": {"field": "entity_type", "size": _CATALOG_AGG_SIZE},
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
    _agg_truncation_warnings(resp["aggregations"])
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
            "aggs": {
                "types": {"terms": {"field": "entity_type", "size": _CATALOG_AGG_SIZE}}
            },
        },
    )
    _agg_truncation_warnings(resp["aggregations"])
    return [b["key"] for b in resp["aggregations"]["types"]["buckets"]]


# Object paths that may fold onto each other in describe_fields (#452, #492): one
# family per meaning, so only the same field repeated elsewhere folds (an NPC stat
# fragment under summon_stats / variants / phases / ng_plus, a weapon stat at
# max_level, a skill table per class, the PvP poise tables). Equal shapes alone
# don't fold: resistances and status_buildup share keys but not a meaning. Objects
# built on the shared condition dicts (_SET_WHEN, _QUEST_CONDITION) form their own
# families (_fold_families).
_FOLD_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("summon_stats", "max_level.summon_stats"),
    ("stats_scaled", "variants.stats_scaled"),
    (
        "resistances",
        "variants.resistances",
        "phases.resistances",
        "summon_stats.resistances",
        "npc_param_values.resistances",
        "stats_scaled.resistances",
        "stats_scaled.ng_plus.resistances",
        "phases.stats_scaled.resistances",
        "phases.stats_scaled.ng_plus.resistances",
    ),
    (
        "defense",
        "variants.defense",
        "phases.defense",
        "summon_stats.defense",
        "stats_scaled.defense",
        "stats_scaled.ng_plus.defense",
        "phases.stats_scaled.defense",
        "phases.stats_scaled.ng_plus.defense",
    ),
    ("negation", "variants.negation", "phases.negation"),
    (
        "resistance_increase_per_proc",
        "variants.resistance_increase_per_proc",
        "phases.resistance_increase_per_proc",
    ),
    (
        "resistance_multiplier_per_proc",
        "variants.resistance_multiplier_per_proc",
        "phases.resistance_multiplier_per_proc",
    ),
    (
        "scripted_damage_taken",
        "variants.scripted_damage_taken",
        "phases.scripted_damage_taken",
    ),
    ("critical_hits", "phases.critical_hits"),
    ("spell_attacks", "summon_stats.spell_attacks"),
    ("weapon_attacks", "summon_stats.weapon_attacks"),
    ("equipment", "summon_stats.equipment"),
    ("attack_power", "max_level.attack_power", "variants.attack_power"),
    ("scaling", "max_level.scaling", "variants.scaling"),
    ("status_buildup", "max_level.status_buildup"),
    ("guard", "max_level.guard"),
    (
        "poise_damage.one_handed",
        "poise_damage.two_handed",
        "poise_damage.pvp.one_handed",
        "poise_damage.pvp.two_handed",
    ),
    (
        "skill.attacks",
        "skill.attacks_by_class.attacks",
        "skill.attacks_by_hand.attacks",
        "skill.attacks_by_class.attacks_by_hand.attacks",
    ),
    ("skill.attacks_by_hand", "skill.attacks_by_class.attacks_by_hand"),
    ("skill.effects", "skill.effects_by_class.effects"),
    (
        "skill.frames",
        "skill.frames_by_class.frames",
        "skill.frames_by_hand.frames",
        "skill.frames_by_class.frames_by_hand.frames",
    ),
    ("skill.frames_by_hand", "skill.frames_by_class.frames_by_hand"),
)


def _fold_families(props: dict) -> dict[str, int]:
    """{object path: fold family} for describe_fields folding (#492).

    The _FOLD_FAMILIES paths, plus one family per shared condition dict: every
    object whose properties are _SET_WHEN (unlock_set_when, gate_set_when, ...) or
    _QUEST_CONDITION (steps.placements.when) or _QUEST_WHEN (steps.when,
    outcomes.when: the same plus flag_range's leaves, #512/#514).
    """
    out = {p: i for i, fam in enumerate(_FOLD_FAMILIES) for p in fam}
    shared_dicts = (_SET_WHEN, _QUEST_CONDITION, _QUEST_WHEN)
    for i, shared in enumerate(shared_dicts, len(_FOLD_FAMILIES)):
        out.update(
            {
                path: i
                for path, spec in _mapping_paths(props).items()
                if spec.get("properties") is shared
            }
        )
    return out


def _folded_copies(props: dict) -> dict[str, tuple[str, int]]:
    """{copy root: (canonical root of the same shape, paths under it)} (#452).

    Objects of one fold family (_fold_families) whose properties are equal share
    a shape. Larger shapes fold first, so a copy's own sub-objects aren't folded
    again; within a shape the shallowest path, then the first in catalog order, is
    the canonical one listed in full, and every other occurrence is a copy.
    """
    families = _fold_families(props)
    shapes: dict[tuple[int, str], list[str]] = {}
    sizes: dict[tuple[int, str], int] = {}
    for path, spec in _mapping_paths(props).items():
        if "properties" not in spec or path not in families:
            continue
        key = (
            families[path],
            json.dumps(spec["properties"], sort_keys=True, default=str),
        )
        shapes.setdefault(key, []).append(path)
        sizes[key] = len(_mapping_paths(spec["properties"]))
    out: dict[str, tuple[str, int]] = {}
    for shape in sorted(shapes, key=lambda s: -sizes[s]):
        live = [
            p
            for p in shapes[shape]
            if not any(p.startswith(root + ".") for root in out)
        ]
        if len(live) < 2:
            continue
        canon = min(live, key=lambda p: p.count("."))
        out.update({p: (canon, sizes[shape]) for p in live if p != canon})
    return out


# The default describe_fields listing (#492): top-level fields only, notes cut to
# a first clause of at most this many characters.
_SHORT_NOTE_CHARS = 100
_TOP_LEVEL_HINT = (
    "top-level fields only; note is the first clause of the field's note and "
    "nested_fields counts the dotted paths under an object. "
    "describe_fields(prefix=<field>) lists a field with its full note and every "
    "path under it (a list of prefixes works too); full=True lists every path "
    "(~2,500 rows, ~360 KB)"
)


def _short_note(note: str) -> str:
    """The note's first clause (up to a '. ' or '; '), capped at _SHORT_NOTE_CHARS.

    A leading "not searchable; returned by get_entity." is skipped: the entry's
    searchable: false already says so.
    """
    note = note.removeprefix("not searchable; returned by get_entity. ")
    clause = re.split(r"(?<!e\.g)(?<!i\.e)(?<=[\w)])[.;] ", note, maxsplit=1)[0]
    if len(clause) <= _SHORT_NOTE_CHARS:
        return clause
    return clause[: _SHORT_NOTE_CHARS - 3].rstrip(" ,") + "..."


def _top_level_fields(props: dict) -> dict[str, dict]:
    """{top-level field: {type, nested_fields?, subfields?, searchable?, note?}}."""
    fields: dict[str, dict] = {}
    for name, spec in props.items():
        entry: dict = {"type": spec.get("type", "object")}
        if "properties" in spec:
            entry["nested_fields"] = len(_mapping_paths(spec["properties"]))
        if spec.get("fields"):
            entry["subfields"] = list(spec["fields"])
        if spec.get("enabled") is False:
            entry["searchable"] = False
        if name in _FIELD_NOTES:
            entry["note"] = _short_note(_FIELD_NOTES[name])
        fields[name] = entry
    return fields


def describe_index(
    client: OpenSearch,
    *,
    prefix: str | list[str] | None = None,
    full: bool = False,
) -> dict:
    """Introspect the index: entity types, internal sources, and the field catalog.

    Lets a caller discover what is queryable without guessing. Field types come
    from the index mapping; entity_types and sources are counted live.

    Without ``prefix`` or ``full`` only the top-level fields are listed, each with
    its type, a short note and nested_fields (#492). ``prefix`` (a dotted path or a
    list of them; a trailing ".*" is ignored) lists only those paths and the ones
    under them with full notes; there a repeated object of one fold family
    (_fold_families) is folded into one row on its root, {"type": "object",
    "same_shape_as": <first path of that shape>, "folded_paths": N}, and a path
    under it keeps its own row only where its note differs from the one at the
    same place under same_shape_as. A copy the prefix names or reaches into is
    unfolded. ``full`` lists every path unfolded (#452).
    """
    resp = client.search(
        index=INDEX,
        body={
            "size": 0,
            "aggs": {
                "types": {"terms": {"field": "entity_type", "size": _CATALOG_AGG_SIZE}},
                "sources": {"terms": {"field": "source", "size": _CATALOG_AGG_SIZE}},
            },
        },
    )
    warnings = _agg_truncation_warnings(resp["aggregations"])
    entity_types = {
        b["key"]: b["doc_count"] for b in resp["aggregations"]["types"]["buckets"]
    }
    sources = {
        b["key"]: b["doc_count"] for b in resp["aggregations"]["sources"]["buckets"]
    }

    props = INDEX_MAPPING["mappings"]["properties"]
    raw = [prefix] if isinstance(prefix, str) else list(prefix or [])
    prefixes = [p.strip().removesuffix(".*").strip(".") for p in raw]
    prefixes = [p for p in prefixes if p]
    if not prefixes and not full:
        out = {
            "entity_types": entity_types,
            "sources": sources,
            "fields": _top_level_fields(props),
            "listing": _TOP_LEVEL_HINT,
        }
        if warnings:
            out["warnings"] = warnings
        return out
    folds = {
        root: canon
        for root, canon in ({} if full else _folded_copies(props)).items()
        if not any(p == root or p.startswith(root + ".") for p in prefixes)
    }

    def _folded_under(path: str) -> str | None:
        """The canonical path matching ``path`` when it lies under a fold."""
        for root, (canon, _) in folds.items():
            if path.startswith(root + "."):
                return canon + path[len(root) :]
        return None

    fields: dict[str, dict] = {}
    for path, spec in _mapping_paths(props).items():
        if prefixes and not any(
            path == p or path.startswith(p + ".") for p in prefixes
        ):
            continue
        twin = _folded_under(path)
        if twin is not None and (
            path not in _FIELD_NOTES or _FIELD_NOTES[path] == _FIELD_NOTES.get(twin)
        ):
            continue
        entry: dict = {"type": spec.get("type", "object")}
        if path in folds:
            entry["same_shape_as"], entry["folded_paths"] = folds[path]
        subs = list(spec.get("fields", {}))
        if subs:
            entry["subfields"] = subs
        # Stored in _source, not indexed: get_entity / include_fields only (#446).
        if spec.get("enabled") is False:
            entry["searchable"] = False
        if path in _FIELD_NOTES:
            entry["note"] = _FIELD_NOTES[path]
        fields[path] = entry

    for p in prefixes:
        if not any(f == p or f.startswith(p + ".") for f in fields):
            warnings.append(f"no mapped field matches prefix {p!r}")
    out = {"entity_types": entity_types, "sources": sources, "fields": fields}
    if any("same_shape_as" in e for e in fields.values()):
        out["folded"] = (
            "a row with same_shape_as stands for an object with the same fields "
            "as that path (folded_paths of them, listed there); "
            "describe_fields(prefix=<its path>) lists them under it, full=True "
            "lists every path"
        )
    if warnings:
        out["warnings"] = warnings
    return out
