"""OpenSearch client, index setup and bulk helpers shared by the loader scripts.

``load_dataset.py`` (loads one dataset.json), ``load_manifest.py`` (incremental
loads) and ``index_settings.py`` (load-time refresh/translog settings) import
from here. The erdb/Discord/FTS loader retired on 2026-09-21 used to share
``load_data.py`` with these helpers; it was deleted in #444 and is in the git
history.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any

from opensearchpy import OpenSearch, RequestError
from opensearchpy.helpers import bulk, scan

from elden_ring._client import tls_options  # pinned node CA (#467)
from elden_ring._schema import INDEX, INDEX_MAPPING  # reuse index name + mapping

# ---------------------------------------------------------------------------
# OpenSearch client
# ---------------------------------------------------------------------------


def _env_on(name: str) -> bool:
    """Load-tuning knobs (#406) default on; ``NAME=0`` (or false/no/off) turns one off."""
    return os.environ.get(name, "1").strip().lower() not in ("0", "false", "no", "off")


def _get_client() -> OpenSearch:
    endpoint = os.environ.get("OPENSEARCH_ENDPOINT")
    user = os.environ.get("OPENSEARCH_USER", "admin")
    password = os.environ.get("OPENSEARCH_PASSWORD")
    if not endpoint:
        sys.exit("OPENSEARCH_ENDPOINT env var is required")
    if not password:
        sys.exit("OPENSEARCH_PASSWORD env var is required")
    return OpenSearch(
        hosts=[{"host": endpoint, "port": 9200}],
        http_auth=(user, password),
        **tls_options(),  # verify against the pinned node CA (#467)
        timeout=60,
        # a transient read timeout or 429 retries instead of failing a whole
        # version (#448); index and delete by _id are idempotent
        retry_on_timeout=True,
        max_retries=3,
        # gzip the bulk bodies (~11x smaller, #406); ER_LOAD_COMPRESS=0 turns it off
        http_compress=_env_on("ER_LOAD_COMPRESS"),
    )


# ---------------------------------------------------------------------------
# Index / bulk load
# ---------------------------------------------------------------------------


# The alias the MCP server reads (#115). A load never creates a concrete index
# under this name: if the alias is missing, that index would shadow it (#448).
_DEFAULT_ALIAS = "elden-ring-entities"


class IndexMissing(RuntimeError):
    """``INDEX`` is the default alias name and doesn't exist (#448)."""


def ensure_index(client: OpenSearch, recreate: bool = False) -> None:
    """Create ``INDEX`` if it's missing, else update its field limit and mapping.

    Two parallel loads (LOAD_JOBS=2) can both see a new index as missing; the
    loser's ``resource_already_exists_exception`` counts as success and it goes
    on to put the mapping like any existing index (#448). Raises
    ``IndexMissing`` rather than auto-create under the default alias name: set
    ELDEN_RING_INDEX to the new concrete index to create one, then alias it.
    """
    if recreate and client.indices.exists(index=INDEX):
        print(f"  Deleting index '{INDEX}' for recreation …")
        client.indices.delete(index=INDEX)
    if not client.indices.exists(index=INDEX):
        if INDEX == _DEFAULT_ALIAS:
            raise IndexMissing(
                f"'{INDEX}' doesn't exist: refusing to create a concrete index "
                "under the alias name. Point the alias at an index, or set "
                "ELDEN_RING_INDEX to the concrete index to create."
            )
        print(f"  Creating index '{INDEX}' …")
        try:
            client.indices.create(index=INDEX, body=INDEX_MAPPING)
            return
        except RequestError as e:
            if e.error != "resource_already_exists_exception":
                raise
            print(f"  Index '{INDEX}' was created by a parallel load.")
    print(f"  Index '{INDEX}' already exists, updating mapping …")
    limit = INDEX_MAPPING["settings"]["mapping.total_fields.limit"]
    client.indices.put_settings(
        index=INDEX, body={"index.mapping.total_fields.limit": limit}
    )
    client.indices.put_mapping(index=INDEX, body=INDEX_MAPPING["mappings"])


def _concrete_indices(client: OpenSearch) -> list[str]:
    """The concrete index (or indices) behind ``INDEX``, which is usually an alias
    (#115); empty when it doesn't exist yet."""
    if not client.indices.exists(index=INDEX):
        return []
    return sorted(client.indices.get_settings(index=INDEX))


# Index settings a reindex changes while it loads (#406). ``None`` resets a setting
# to the OpenSearch default (refresh 1s, per-request translog fsync), which is what
# the live index runs with, so a restore needs no record of the previous values.
_PAUSED_SETTINGS = {
    "index.refresh_interval": "-1",
    "index.translog.durability": "async",
}


def _pause_settings() -> dict:
    """The settings ``pause_refresh`` applies under the env knobs:
    ER_LOAD_PAUSE_REFRESH=0 skips the refresh pause, ER_LOAD_ASYNC_TRANSLOG=0
    keeps per-request translog fsync."""
    body = {}
    if _env_on("ER_LOAD_PAUSE_REFRESH"):
        body["index.refresh_interval"] = _PAUSED_SETTINGS["index.refresh_interval"]
    if _env_on("ER_LOAD_ASYNC_TRANSLOG"):
        body["index.translog.durability"] = _PAUSED_SETTINGS[
            "index.translog.durability"
        ]
    return body


def pause_refresh(client: OpenSearch) -> list[str]:
    """Stop periodic refreshes (and fsync the translog asynchronously) on the
    concrete index for a bulk reindex. Searches keep serving the last refreshed
    view; ``restore_refresh`` must run afterwards. Returns the indices changed."""
    body = _pause_settings()
    if not body:
        print("  Refresh pause and async translog are both turned off; no change.")
        return []
    indices = _concrete_indices(client)
    for name in indices:
        client.indices.put_settings(index=name, body=body)
        print(f"  Paused '{name}': {body}")
    if not indices:
        print(f"  Index '{INDEX}' doesn't exist yet; nothing to pause.")
    return indices


def restore_refresh(client: OpenSearch) -> list[str]:
    """Reset what ``pause_refresh`` changed to the defaults and force a refresh so
    every loaded and pruned doc is searchable. Safe to run at any time (a
    reindex runs it at startup too, in case a killed run left refresh off)."""
    indices = _concrete_indices(client)
    for name in indices:
        client.indices.put_settings(index=name, body=dict.fromkeys(_PAUSED_SETTINGS))
        client.indices.refresh(index=name)
        print(f"  Restored refresh/translog defaults on '{name}' and refreshed it.")
    if not indices:
        print(f"  Index '{INDEX}' doesn't exist; nothing to restore.")
    return indices


def _to_bulk_actions(docs: list[dict]) -> list[dict]:
    return [
        {
            "_index": INDEX,
            # Unique ID: entity_type::name::patch_version
            # Using name (not internal row ID) so the same weapon across patches
            # gets the same logical key when patch_version matches.
            "_id": f"{d['entity_type']}::{d['name']}::{d.get('patch_version', 'unknown')}",
            # entity_key is the search collapse/dedupe/count key (#443).
            "_source": {k: v for k, v in d.items() if v is not None}
            | {"entity_key": f"{d['entity_type']}::{d['name']}"},
        }
        for d in docs
        if d.get("name")
    ]


# Docs per bulk request; ER_BULK_CHUNK_SIZE lets a reindex try larger ones (#406).
_BULK_CHUNK_SIZE = int(os.environ.get("ER_BULK_CHUNK_SIZE") or 500)

# Retry a chunk's 429 (too many requests) items with 2s, 4s, 8s backoff instead
# of failing the version (#448); writes by _id are idempotent.
_BULK_RETRY: dict[str, Any] = {"max_retries": 3, "initial_backoff": 2}


def load_documents(
    client: OpenSearch | None, docs: list[dict], dry_run: bool
) -> tuple[int, list]:
    actions = _to_bulk_actions(docs)
    if not actions:
        print("  No documents to index.")
        return 0, []
    if dry_run:
        print(f"  [dry-run] Would index {len(actions)} documents.")
        print(json.dumps(actions[:2], indent=2, ensure_ascii=False))
        return 0, []
    success, errors = bulk(
        client,
        actions,
        raise_on_error=False,
        chunk_size=_BULK_CHUNK_SIZE,
        **_BULK_RETRY,
    )
    print(f"  Indexed {success} documents.")
    if errors:
        print(f"  {len(errors)} errors (first 3):")
        for e in errors[:3]:
            print(f"    {e}")
    return success, errors


# Refuse to prune when a reload would retract more than this share of an entity
# type's docs at one patch — that signals a truncated/broken build, not stale
# leftovers (the worst real case, #67's 1.02 enemies, is ~21%). The share is
# stale / (stale + built), which doesn't depend on whether the docs just written
# are refreshed yet (#448); for a truncated build it equals stale / old live.
_PRUNE_MAX_FRACTION = 0.5


class PruneRefused(RuntimeError):
    """A prune would delete more than the allowed share of a type's docs; nothing
    was deleted (#448)."""


def _stale_ids(live_ids, docs: list[dict]) -> list[str]:
    """Live ``_id``s that a rebuild of ``docs`` no longer produces."""
    built = {a["_id"] for a in _to_bulk_actions(docs)}
    return [i for i in live_ids if i not in built]


def prune_stale(
    client: OpenSearch | None,
    docs: list[dict],
    dry_run: bool,
    max_fraction: float = _PRUNE_MAX_FRACTION,
) -> tuple[int, list]:
    """Delete docs a reload no longer produces, scoped per patch_version to the
    entity_types present in ``docs`` (#67).

    The loader is additive (overwrite-by-_id), so a doc written by an earlier,
    wrong build — e.g. DLC enemies stamped onto 1.02 from 1.17 NpcName — survives
    every correct rebuild. Other patches and entity_types absent from ``docs``
    (e.g. npc_dialogue on an --items-only load) are never touched. Raises
    ``PruneRefused`` without deleting if any type's stale docs are more than
    ``max_fraction`` of its stale + built docs at a version (``1`` never refuses).

    Returns ``(deleted, errors)`` like ``load_documents``, so callers can fail the
    run on delete errors instead of mistaking a partial prune for success (#415).
    """
    scopes: dict[str, set[str]] = defaultdict(set)
    for d in docs:
        if d.get("name"):
            scopes[d.get("patch_version", "unknown")].add(d["entity_type"])

    stale: list[str] = []
    for version, types in sorted(scopes.items()):
        live = [
            h["_id"]
            for h in scan(
                client,
                index=INDEX,
                _source=False,
                query={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"patch_version": version}},
                                {"terms": {"entity_type": sorted(types)}},
                            ]
                        }
                    }
                },
            )
        ]
        version_stale = _stale_ids(live, docs)
        # The built side counts the dataset's own docs, not the live ones: the
        # docs this load just wrote are only live after a refresh, so a live
        # count would make the guard depend on the refresh setting (#448).
        built_by_type: dict[str, set[str]] = defaultdict(set)
        stale_by_type: dict[str, list[str]] = defaultdict(list)
        for a in _to_bulk_actions(docs):
            if a["_source"].get("patch_version", "unknown") == version:
                built_by_type[a["_source"]["entity_type"]].add(a["_id"])
        for i in version_stale:
            stale_by_type[i.split("::", 1)[0]].append(i.split("::")[1])
        for t, names in sorted(stale_by_type.items()):
            built = len(built_by_type[t])
            total = len(names) + built
            print(
                f"  [{version}] stale {t}: {len(names)} (built {built}; "
                f"e.g. {names[:3]})"
            )
            if len(names) > max_fraction * total:
                raise PruneRefused(
                    f"refusing to prune: {len(names)} of {total} {t} docs at "
                    f"{version} (stale + built) would be deleted, over "
                    f"{max_fraction:.0%}; the build looks truncated. Nothing was "
                    "deleted; rerun with --force-prune if it is intended."
                )
        stale += version_stale

    if not stale:
        print("  No stale documents.")
        return 0, []
    if dry_run:
        print(f"  [dry-run] Would delete {len(stale)} stale documents.")
        return 0, []
    deleted, errors = bulk(
        client,
        [{"_op_type": "delete", "_index": INDEX, "_id": i} for i in stale],
        raise_on_error=False,
        chunk_size=500,
        **_BULK_RETRY,
    )
    print(f"  Pruned {deleted} stale documents.")
    if errors:
        print(f"  {len(errors)} delete errors (first 3): {errors[:3]}")
    return deleted, errors
