"""Stage 2 of the native pipeline: dataset.json → OpenSearch.

Reads the JSON produced by er_native/build_dataset.py and bulk-indexes it,
reusing this package's existing client/index/bulk helpers. Additive by default:
docs are keyed ``entity_type::name::patch_version`` so loading one patch never
touches other patches' documents.

``--prune`` also deletes docs at the dataset's patch version(s) that the dataset
no longer produces, scoped to the entity_types it contains (#67). Without it, a
doc written by an earlier wrong build survives every correct reload.

Exits non-zero when any document fails to index, whatever the flags (#411).

``--manifest-dir DIR`` indexes only the docs whose content changed since the last
load into the same concrete index (an ``_id -> hash`` manifest per patch in DIR,
written after a clean load; see load_manifest.py, #409). ``--full`` indexes every
doc anyway and rewrites the manifest.

    OPENSEARCH_ENDPOINT=... OPENSEARCH_PASSWORD=... \
        python load_dataset.py path/to/dataset.json [--manifest-dir DIR [--full]] [--prune]
"""

from __future__ import annotations

import argparse
import json
import sys

from load_data import _get_client, ensure_index, load_documents, prune_stale
from load_manifest import record, select_changed, target_index


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
    args = ap.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        docs = json.load(f)
    if not isinstance(docs, list) or not docs:
        sys.exit("dataset is empty or not a list of documents")

    patches = sorted({d.get("patch_version") for d in docs})
    print(f"Loaded {len(docs)} docs (patches: {patches})")

    # A dry-run prune still needs a (read-only) client to find stale docs.
    client = None if args.dry_run and not args.prune else _get_client()
    # No recreate — additive load, preserves existing patch versions.
    if not args.dry_run:
        ensure_index(client, recreate=False)
    index = None
    to_load = docs
    if args.manifest_dir and client is not None:
        index = target_index(client)
        to_load = select_changed(client, docs, args.manifest_dir, index, args.full)
    _, errors = load_documents(client, to_load, dry_run=args.dry_run)
    if args.prune:
        if errors:
            sys.exit("load had errors — skipping prune so no live doc is lost")
        prune_stale(client, docs, dry_run=args.dry_run)
    # Any failed doc fails the run, with or without --prune (#411), so callers
    # that check the exit status can't mistake a partial load for success.
    if errors:
        sys.exit(f"load had {len(errors)} errors")
    # Only a clean load (+ prune) is recorded, so a failure never hides a doc (#409).
    if index and not args.dry_run:
        record(docs, args.manifest_dir, index)


if __name__ == "__main__":
    main()
