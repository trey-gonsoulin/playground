# elden-ring

An MCP server that searches Elden Ring game data at every patch: items, enemies, NPCs, quests, locations and dialogue. The data lives in an OpenSearch index on an EC2 instance in the `mcp-playground` stack. The Lambda that reads the index now deploys from soulsearch (`SoulsearchMcpFunction` in its `soulsearch` stack, soulsearch#2), and `start_search_service` starts the instance on demand. That tool returns within about 20 s, so a cold start takes a few calls before it answers `ready`. The `EldenRingSearchIdleStopAlarm` alarm stops the instance after an hour below 2% average CPU.

The repo-level summary is the elden-ring section of the root `CLAUDE.md`. This file is the runbook.

## Layout

- `elden_ring/_client.py`, top to bottom:
  - the connection: `get_client` (EC2 public DNS plus the SSM password) and `start_instance`;
  - the query layer behind the tools: `search`, `get_entity`, the literal-search and patch-diff helpers, and `describe_index`.
- `elden_ring/_schema.py`: the schema, `INDEX` (`ELDEN_RING_INDEX`, default alias `elden-ring-entities`), `INDEX_MAPPING` (`dynamic: strict`) and `KUROMOJI_USER_DICTIONARY`, plus the mapping walkers `_mapping_paths` and `_disabled_paths`.
- `elden_ring/_field_notes.py`: `_FIELD_NOTES` (served by `describe_fields`).
- `elden_ring/mcp_server.py`: the twelve tools and their docstrings.
- `elden_ring/_calc.py`: the attack-rating math behind `calculate_attack_rating`.
- `scripts/load_dataset.py`: loads one `dataset.json` into OpenSearch. `scripts/opensearch_load.py` holds the shared client, `ensure_index` and bulk helpers (the retired erdb loader that shared `load_data.py` with them was deleted in #444). `load_manifest.py` and `index_settings.py` support incremental loads and the load-time settings.
- `tests/`: offline unit tests, plus the opt-in live suite (see Tests).

## Data flow

1. **Extract** (`~/code/erdb-tools`, a separate repo). `er_native/build_dataset.py` reads one patch's decrypted regulation, FMG text, EMEVD/ESD scripts and MSB placements from `datalake/`, and writes a `dataset.json`.
2. **Load** (this package). `scripts/load_dataset.py <dataset.json>` bulk-indexes it. Docs are keyed `entity_type::name::patch_version`, so a load never touches other patches.
   - `--prune` deletes the docs at the dataset's patch(es), for the entity types it contains, that the dataset no longer produces. Without it, docs from an earlier wrong build survive every reload.
   - `--manifest-dir DIR` indexes only docs whose content hash changed since the last clean load into the same concrete index. `--full` indexes every doc anyway and rewrites the manifest.
   - It exits non-zero when any doc fails to index.
3. **Reindex everything** (erdb-tools). `er_native/reindex_all.sh` builds and loads every patch with `--manifest-dir datalake/.load_manifest --prune`. It looks up the endpoint and password from AWS itself. Run it detached and keep the Mac awake:

   ```bash
   cd ~/code/erdb-tools
   nohup caffeinate -is env JOBS=10 er_native/reindex_all.sh > reindex.log 2>&1 < /dev/null &
   ```

   - `JOBS` is the build-pool size. Loads stay at 2 (`LOAD_JOBS`), which is what the t4g.small node can take. A full run takes about 7–8 minutes.
   - The log ends with `reindex complete`. Check it for `BUILD FAILED` / `LOAD FAILED`, and rerun only the failed versions: `er_native/reindex_all.sh <v1> <v2> …`.
   - `--force` reruns the prelude steps even when their inputs are unchanged.
   - `--full` rewrites docs that haven't changed. Use it after a mapping-only change, or after an ad-hoc load that didn't pass `--manifest-dir`.

For an ad-hoc load, set `OPENSEARCH_ENDPOINT` to the instance's bare public DNS name (not a URL) and `OPENSEARCH_PASSWORD` to the SSM parameter `/elden-ring/opensearch-password`. Then run:

```bash
uv run --package elden-ring python packages/elden-ring/scripts/load_dataset.py dataset.json --prune
```

## Index rules

- **The alias.** `elden-ring-entities` is an alias to one concrete index (currently `elden-ring-entities-v4`). The Lambda and the loaders use the alias unless `ELDEN_RING_INDEX` says otherwise.
- **Map new fields first.** The mapping is `dynamic: strict`, so a doc with an unmapped leaf (nested ones included) is rejected and the load fails.
  - Add every new field to `INDEX_MAPPING` (`_schema.py`), and add a `_FIELD_NOTES` entry for it (`_field_notes.py`), before loading.
  - `ensure_index` runs `put_mapping` on every load, so new fields reach the live index by themselves.
  - `mapping.total_fields.limit` is 3000. Renamed fields keep their old leaves in the live mapping until the index is rebuilt.
- **Type and analyzer changes need a new concrete index.** That covers a field's type, an analyzer, or `KUROMOJI_USER_DICTIONARY`. The procedure:
  1. Pre-create the new index: `ensure_index` with `ELDEN_RING_INDEX=elden-ring-entities-v<N>`.
  2. Load it in full with `ELDEN_RING_INDEX=elden-ring-entities-v<N> er_native/reindex_all.sh`. A new index has no manifest, so every doc loads, and the per-patch dialogue loads with it.
  3. Compare the per-`entity_type` and per-patch counts with the old index.
  4. Swap the alias atomically: one `update_aliases` call with a remove and an add.
  5. Delete the old index only once the new one is verified. Nothing else keeps a copy.

## Tests

```bash
uv run --package elden-ring pytest packages/elden-ring/tests/   # offline; no AWS or OpenSearch
packages/elden-ring/scripts/check.sh                            # the quality gate (below)
```

There is no CI, so run `scripts/check.sh` before pushing. It runs the locked tools from the `lint` dependency group (`--group lint`): `ruff check`, `ruff format --check`, `pyright` (basic mode), then the offline tests with `--cov-fail-under=45`. The `lint` group is not installed by default, so the plain pytest command above never needs its wheels. Once they are cached, `check.sh --offline` works too. The ruff rule set, pyright settings and per-file waivers are in this package's `pyproject.toml`. Other arguments also go to `uv run`. The loader scripts are on the test path (`pythonpath = ["scripts"]`), so tests import `opensearch_load` and friends directly.

`uv.lock` resolves against PyPI, and the Dockerfiles install from it with `--require-hashes` (#454). A shell that exports `UV_DEFAULT_INDEX` overrides the workspace's index, and an unfrozen `uv run` or `uv lock` then rewrites the lock with that index's URLs. In such a shell, run with `--frozen`, or relock with `uv lock --default-index https://pypi.org/simple`.

Audit the locked runtime dependencies now and then, and after any lock change:

```bash
uv export --package elden-ring --no-dev --frozen --no-emit-workspace -o /tmp/er-reqs.txt
uvx pip-audit -r /tmp/er-reqs.txt --disable-pip
```

Upgrade flagged packages with `uv lock --upgrade-package <name>`, then rebuild and redeploy.

The live integration suite is opt-in. Its tests carry the pytest marker `live` (deselected by default) and only run with `ER_LIVE_TESTS=1`. The module fixture creates its own throwaway `elden-ring-it-<uuid>` index from `INDEX_MAPPING`, points the client and loader at it, deletes it at teardown, and refuses to run against an alias or an existing index:

```bash
ER_LIVE_TESTS=1 uv run --package elden-ring pytest packages/elden-ring/tests/ -m live
```

They also need the Lambda's environment: `EC2_INSTANCE_ID`, `OPENSEARCH_USER` and `OPENSEARCH_PASSWORD_SSM_PATH`. A killed run can leave an `elden-ring-it-*` index behind; delete it by hand.

## Deploy

The MCP Lambda and its HTTP API moved to soulsearch's own `soulsearch` stack (soulsearch#2); deploy server code from there (see soulsearch's `packages/soulsearch-mcp/README.md#deploy`). This stack still holds the OpenSearch instance, its security group, role and idle-stop alarm (moving them is soulsearch#3). Deploy a change to those from the repo root:

```bash
export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock
sam build
sam deploy --no-execute-changeset
aws cloudformation execute-change-set --change-set-name <arn> --profile personal
aws cloudformation wait stack-update-complete --stack-name mcp-playground --profile personal
```

- **Export `DOCKER_HOST`; don't prefix it.** `sam build` and `sam deploy` must both use Docker Desktop's engine, and this Mac has a second engine. On 2026-09-28 an inline `DOCKER_HOST=… sam build && sam deploy` pushed a stale image from that engine and rolled the live code back. If the socket is missing, start Docker Desktop first.
- **Review the change set before executing it.** Expect only the rows you changed, Modify, with Replacement False. Any `EldenRingSearchInstance` row with Replacement True or Conditional is a stop sign, because a new instance starts with an empty index. A stack policy also denies replacing or deleting that instance.
- **Keep the instance id stable.** The `soulsearch` stack takes it as a parameter (this stack's `EldenRingSearchInstanceId` output). If the instance is ever replaced, update soulsearch's `samconfig.toml` and redeploy that stack.
- **Never run `sam delete`.** It targets the whole `mcp-playground` stack, including the instance that holds the index.

## Rebuilding the search instance

UserData runs only on the instance's first boot. Rebuilding means a new instance, and a new instance starts with an empty index. The steps:

1. The SSM SecureString `/elden-ring/opensearch-password` must exist. UserData reads it for the admin user, and so does the Lambda.
2. UserData installs everything (see `EldenRingSearchInstance` in `template.yaml`):
   - OpenSearch 2.13 and the `analysis-kuromoji` plugin, which the index settings require;
   - a private CA with node and admin certs in place of the demo certs, and `admin` as the only internal user (#437, #450);
   - a 512 MB heap, 2 GB swap and single-node discovery.
3. The AMI is pinned (`ImageId`), because a new AMI forces a replacement. To upgrade on purpose, change it, deploy with `--stack-policy-during-update-body` to lift the replace denial for that update, and plan a full reindex.
4. The root volume has `DeleteOnTermination: false`, so a replaced instance leaves its old volume behind. Delete that volume by hand once the new index is verified.
5. Rebuild the index from scratch: `ensure_index`, then a full `reindex_all.sh`. No backup index exists.
