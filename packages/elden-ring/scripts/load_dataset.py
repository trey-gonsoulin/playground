"""Stage 2 of the native pipeline: dataset.json → OpenSearch.

Reads the JSON produced by er_native/build_dataset.py and bulk-indexes it,
reusing this package's existing client/index/bulk helpers. Additive by default:
docs are keyed ``entity_type::name::patch_version`` so loading one patch never
touches other patches' documents.

``--prune`` also deletes docs at the dataset's patch version(s) that the dataset
no longer produces, scoped to the entity_types it contains (#67). Without it, a
doc written by an earlier wrong build survives every correct reload. The prune
refuses (exit non-zero, nothing deleted) when a type's stale docs are more than
half of its stale + built docs at a version, which signals a truncated build;
``--prune-max-fraction F`` moves that bar and ``--force-prune`` lifts it (#448).

Before connecting, every doc is checked against the strict mapping: a field
INDEX_MAPPING doesn't have exits non-zero listing its dotted paths, on real and
``--dry-run`` loads alike, so an unmapped leaf no longer fails per doc part-way
through a reindex (#448).

Exits non-zero when any document fails to index, whatever the flags (#411).

``--manifest-dir DIR`` indexes only the docs whose content changed since the last
load into the same concrete index (an ``_id -> hash`` manifest per patch in DIR,
written after a clean load; see load_manifest.py, #409). ``--full`` indexes every
doc anyway and rewrites the manifest.

    OPENSEARCH_ENDPOINT=... OPENSEARCH_PASSWORD=... \
        python load_dataset.py path/to/dataset.json [--manifest-dir DIR [--full]] \
            [--force-prune | --prune-max-fraction F] [--prune]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from elden_ring._client import unmapped_paths
from load_data import (
    _PRUNE_MAX_FRACTION,
    IndexMissing,
    PruneRefused,
    _get_client,
    ensure_index,
    load_documents,
    prune_stale,
)
from load_manifest import forget, record, select_changed, target_index


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Index a native dataset.json into OpenSearch."
    )
    ap.add_argument("dataset", help="Path to dataset.json from build_dataset.py")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be indexed, don't write.",
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="After a clean load, delete docs at these patch versions (and "
        "entity types) that the dataset no longer produces.",
    )
    ap.add_argument(
        "--manifest-dir",
        help="Index only docs whose content changed since the last load into the "
        "same concrete index, per the hash manifests kept here (#409).",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="With --manifest-dir: index every doc anyway (after a mapping change "
        "or an outside load) and rewrite the manifest.",
    )
    guard = ap.add_mutually_exclusive_group()
    guard.add_argument(
        "--prune-max-fraction",
        type=float,
        default=_PRUNE_MAX_FRACTION,
        metavar="F",
        help="With --prune: refuse when a type's stale docs are more than F of "
        f"its stale + built docs at a version (default {_PRUNE_MAX_FRACTION}).",
    )
    guard.add_argument(
        "--force-prune",
        action="store_true",
        help="With --prune: delete stale docs however many there are (an "
        "intended mass rename).",
    )
    args = ap.parse_args()
    max_fraction = 1.0 if args.force_prune else args.prune_max_fraction

    with Path(args.dataset).open(encoding="utf-8") as f:
        docs = json.load(f)
    if not isinstance(docs, list) or not docs:
        sys.exit("dataset is empty or not a list of documents")

    patches = sorted({d.get("patch_version") for d in docs})
    print(f"Loaded {len(docs)} docs (patches: {patches})")
    unmapped = unmapped_paths([d for d in docs if d.get("name")])
    if unmapped:
        sys.exit(
            f"{len(unmapped)} field paths aren't in INDEX_MAPPING (strict); add "
            "them before loading: " + ", ".join(unmapped)
        )

    # A dry-run prune still needs a (read-only) client to find stale docs.
    client = None if args.dry_run and not args.prune else _get_client()
    # No recreate — additive load, preserves existing patch versions.
    if client is not None and not args.dry_run:
        try:
            ensure_index(client, recreate=False)
        except IndexMissing as e:
            sys.exit(str(e))
    index = None
    to_load = docs
    if args.manifest_dir and client is not None:
        index = target_index(client)
        to_load = select_changed(client, docs, args.manifest_dir, index, args.full)
        if index and not args.dry_run:
            forget(docs, args.manifest_dir, index)
    _, errors = load_documents(client, to_load, dry_run=args.dry_run)
    if args.prune:
        if errors:
            sys.exit("load had errors — skipping prune so no live doc is lost")
        try:
            _, prune_errors = prune_stale(
                client, docs, dry_run=args.dry_run, max_fraction=max_fraction
            )
        except PruneRefused as e:
            # The new docs are already written; old and new both stay live until
            # a forced prune. Exit non-zero so the reload scripts report it.
            sys.exit(f"  !! {e}")
        if prune_errors:
            sys.exit(f"prune had {len(prune_errors)} delete errors")
    # Any failed doc fails the run, with or without --prune (#411), so callers
    # that check the exit status can't mistake a partial load for success.
    if errors:
        sys.exit(f"load had {len(errors)} errors")
    # Only a clean load (+ prune) is recorded; forget() above already dropped these
    # types, so a failure never hides a doc (#409).
    if index and not args.dry_run:
        record(docs, args.manifest_dir, index)


if __name__ == "__main__":
    main()
