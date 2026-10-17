"""Incremental loads: index only the docs whose content changed (#409).

Builds are deterministic (a local build matches the live ``_source`` exactly), so
a reload mostly rewrites docs with the same content. ``load_dataset.py
--manifest-dir DIR`` keeps a manifest of ``_id -> content hash`` for each concrete
index and patch version, in ``DIR/<concrete index>/<version>.json`` grouped by
entity_type, and on the next load indexes only the docs whose hash differs from the
manifest or that are no longer live.

- The manifest is keyed by the **concrete** index (the alias ``INDEX`` resolves to,
  #115), so an alias swap to a new index has no manifest and loads in full.
- It is written only after a clean load (and prune), replacing just the entity
  types of the loaded dataset at each of its versions; other types (e.g. dialogue
  next to an --items-only load) keep their entries. Those types are dropped from
  the manifest before the load starts, so a failed or killed load is reloaded in
  full next time.
- A live ``_id`` scan of each (version, types) scope (as ``prune_stale`` does)
  reloads any manifest doc that was deleted since, so only an in-place rewrite of
  a doc by some other loader can leave a stale entry. ``--full`` loads every doc
  (and rewrites the manifest): use it after a mapping change on the same index or
  after loading docs some other way.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from load_data import INDEX, _concrete_indices, _to_bulk_actions, scan


def doc_hash(source: dict) -> str:
    """A stable hash of a doc's indexed ``_source`` (key order doesn't matter)."""
    blob = json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def target_index(client) -> str | None:
    """The one concrete index loads go to, else None (no incremental load)."""
    names = _concrete_indices(client)
    return names[0] if len(names) == 1 else None


def _path(manifest_dir, index: str, version: str) -> Path:
    return Path(manifest_dir) / index / f"{version}.json"


def _read(path: Path) -> dict[str, dict[str, str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _hashes(docs: list[dict]) -> dict[str, dict[str, dict[str, str]]]:
    """version -> entity_type -> _id -> hash (the last doc of a repeated _id wins,
    as it does in the bulk load)."""
    out: dict = defaultdict(lambda: defaultdict(dict))
    for a in _to_bulk_actions(docs):
        s = a["_source"]
        out[s.get("patch_version", "unknown")][s["entity_type"]][a["_id"]] = doc_hash(s)
    return out


def _live_ids(client, version: str, types: list[str]) -> set[str]:
    query = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"patch_version": version}},
                    {"terms": {"entity_type": types}},
                ]
            }
        }
    }
    return {h["_id"] for h in scan(client, index=INDEX, _source=False, query=query)}


def select_changed(
    client, docs: list[dict], manifest_dir, index: str | None, full: bool
) -> list[dict]:
    """The docs of ``docs`` to index: all of them with ``full`` or no single
    concrete index, else those whose hash isn't in the manifest or isn't live."""
    if full or index is None:
        why = "--full" if full else f"no single concrete index behind '{INDEX}'"
        print(f"  Full load ({why}).")
        return docs
    changed: set[str] = set()
    total = 0
    for version, by_type in _hashes(docs).items():
        old = _read(_path(manifest_dir, index, version))
        live = _live_ids(client, version, sorted(by_type)) if old else set()
        for t, ids in by_type.items():
            prev = old.get(t, {})
            total += len(ids)
            changed |= {i for i, h in ids.items() if prev.get(i) != h or i not in live}
    named = [d for d in docs if d.get("name")]
    out = [d for d, a in zip(named, _to_bulk_actions(named)) if a["_id"] in changed]
    print(
        f"  Unchanged since the last load into '{index}': {total - len(changed)} of "
        f"{total} docs skipped; {len(changed)} to index."
    )
    return out


def _update(manifest_dir, index: str, version: str, change) -> None:
    path = _path(manifest_dir, index, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A lock per file: a dialogue reload may update the same version's manifest.
    with open(path.with_name(f"{version}.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = change(_read(path))
        tmp = path.with_name(f"{version}.json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def forget(docs: list[dict], manifest_dir, index: str) -> None:
    """Drop the entity types of ``docs`` at each of their versions from the
    manifest before a load writes anything, so a load that fails or is killed
    part-way leaves no hash claiming a doc is current: the next load indexes those
    types in full."""
    for version, by_type in _hashes(docs).items():
        if _path(manifest_dir, index, version).exists():
            _update(
                manifest_dir,
                index,
                version,
                lambda old, types=by_type: {
                    t: ids for t, ids in old.items() if t not in types
                },
            )


def record(docs: list[dict], manifest_dir, index: str) -> None:
    """Write the hashes of ``docs`` (now live) into the manifest: their entity types
    at each of their versions are replaced, everything else is kept."""
    n = 0
    for version, by_type in _hashes(docs).items():
        n += sum(len(ids) for ids in by_type.values())
        _update(manifest_dir, index, version, lambda old, new=by_type: {**old, **new})
    print(f"  Recorded {n} doc hashes in {Path(manifest_dir) / index}.")
