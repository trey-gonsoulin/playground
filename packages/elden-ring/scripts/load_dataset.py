"""Stage 2 of the native pipeline: dataset.json → OpenSearch.

Reads the JSON produced by er_native/build_dataset.py and bulk-indexes it,
reusing this package's existing client/index/bulk helpers. Additive by default:
docs are keyed ``entity_type::name::patch_version`` so loading one patch never
touches other patches' documents.

``--prune`` also deletes docs at the dataset's patch version(s) that the dataset
no longer produces, scoped to the entity_types it contains (#67). Without it, a
doc written by an earlier wrong build survives every correct reload.

    OPENSEARCH_ENDPOINT=... OPENSEARCH_PASSWORD=... \
        python load_dataset.py path/to/dataset.json [--prune]
"""

from __future__ import annotations

import argparse
import json
import sys

from load_data import _get_client, ensure_index, load_documents, prune_stale


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
    _, errors = load_documents(client, docs, dry_run=args.dry_run)
    if args.prune:
        if errors:
            sys.exit("load had errors — skipping prune so no live doc is lost")
        prune_stale(client, docs, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
