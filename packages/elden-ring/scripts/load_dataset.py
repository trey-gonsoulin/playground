"""Stage 2 of the native pipeline: dataset.json → OpenSearch.

Reads the JSON produced by er_native/build_dataset.py and bulk-indexes it,
reusing this package's existing client/index/bulk helpers. Additive by default:
docs are keyed ``entity_type::name::patch_version`` so loading one patch never
touches other patches' documents.

    OPENSEARCH_ENDPOINT=... OPENSEARCH_PASSWORD=... \
        python load_dataset.py path/to/dataset.json
"""
from __future__ import annotations

import argparse
import json
import sys

from load_data import _get_client, ensure_index, load_documents


def main() -> None:
    ap = argparse.ArgumentParser(description="Index a native dataset.json into OpenSearch.")
    ap.add_argument("dataset", help="Path to dataset.json from build_dataset.py")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be indexed, don't write.")
    args = ap.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        docs = json.load(f)
    if not isinstance(docs, list) or not docs:
        sys.exit("dataset is empty or not a list of documents")

    patches = sorted({d.get("patch_version") for d in docs})
    print(f"Loaded {len(docs)} docs (patches: {patches})")

    client = None if args.dry_run else _get_client()
    # No recreate — additive load, preserves existing patch versions.
    if client is not None:
        ensure_index(client, recreate=False)
    load_documents(client, docs, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
