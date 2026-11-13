# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

This repo is a collection of personal MCP servers deployed to AWS Lambda and integrated with Claude Desktop / Claude iOS. Each MCP is its own package under `packages/`, following the same structure as `~/code/hoshi` (the reference implementation). The root `pyproject.toml` is a **uv workspace** with one member per MCP package. `vault-mcp` also runs a server on an on-demand EC2 box behind its Lambda (see Packages).

## Architecture pattern

Each MCP package follows this layout (see `~/code/hoshi/packages/hoshi-api/` as the reference):

```
packages/<name>/
  <name>/
    mcp_server.py      # MCPServer tools — the main thing to write
    app.py             # FastAPI factory that mounts the MCP at /mcp
    lambda_handler.py  # Mangum(app) entry point for Lambda
    main.py            # uvicorn CLI entry for local dev
  Dockerfile           # multi-stage: builder (uv sync) → final Lambda image
  pyproject.toml       # hatchling build, mangum + mcp + fastapi + mcp-lambda deps
```

`packages/mcp-lambda` (module `mcp_lambda`) is the shared FastAPI + Lambda wiring every server uses (#504). It is a library package, not an MCP server.

### MCP server setup

mcp 2.x (`mcp>=2.3,<3`). The server object takes only its name; the transport settings go to `streamable_http_app()`, which `mcp_lambda` calls for you:

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("my-mcp-name")
```

`app.py` builds the FastAPI app with the shared helper:

```python
from mcp_lambda import create_mcp_app
from <pkg>.mcp_server import mcp

app = create_mcp_app(
    mcp,
    title="My MCP",
    description="...",
    auth_env="MY_MCP_SECRET",  # optional: require Authorization: Bearer $MY_MCP_SECRET when set
)
```

`create_mcp_app` mounts the server at `/mcp` and:

- fixes the transport settings: `stateless_http=True` (each Lambda invocation is independent), `json_response=True` (plain JSON, not SSE, which API Gateway needs), `streamable_http_path="/"`, and `host="0.0.0.0"` (mcp's default `127.0.0.1` turns on DNS-rebinding protection, which answers API Gateway's Host header with a 421). Other `streamable_http_app()` settings pass through as keyword arguments, e.g. genealogy's `max_request_body_size` (mcp's default is 4 MiB).
- serves `/mcp` as `/mcp/`, so Starlette doesn't redirect.
- makes the lifespan re-enterable (`ReentrantMCP`). Mangum runs the whole lifespan on every invocation, and the session manager's `run()` is single-use, so each lifespan entry builds a fresh one with `streamable_http_app()`.

Each Dockerfile copies `packages/mcp-lambda/mcp_lambda` next to the package, because `uv export --no-emit-workspace` leaves workspace members out of the requirements.

### Lambda entry point

```python
from mangum import Mangum
from <pkg>.app import app
handler = Mangum(app, lifespan="auto")
```

SAM points `CMD` at `<pkg>.lambda_handler.handler`.

## SAM / deployment

The reference SAM config is in `~/code/hoshi/` (`template.yaml`, `samconfig.toml`). Model new stacks after those files:

- `template.yaml` — `AWS::Serverless::Function` with `PackageType: Image`, `HttpApi` events for `/` and `/{proxy+}`, writable `/tmp` cache via env vars.
- `samconfig.toml` — pins `stack_name`, `region = "us-east-1"`, `capabilities = "CAPABILITY_IAM"`, `image_repositories` after first `sam deploy --guided`.
- Docker build context is the repo root so the Dockerfile can `COPY` workspace files.

Build and deploy with a reviewed change set. The ynab, genealogy and markets functions share the `mcp-playground` stack. The Elden Ring MCP Lambda and its OpenSearch instance deploy from soulsearch's own `soulsearch` stack (soulsearch#2, soulsearch#3):

```bash
export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock   # exported, so sam build AND sam deploy use Docker Desktop's engine
sam build
sam deploy --no-execute-changeset                         # creates and prints the change set, executes nothing
# review it: expect Modify rows with Replacement False
aws cloudformation execute-change-set --change-set-name <arn> --profile personal
aws cloudformation wait stack-update-complete --stack-name mcp-playground --profile personal
```

- Afterwards, check that the deployed image tags match the local images: the `<function>-<12hex>-latest` tag in `aws cloudformation get-template --stack-name mcp-playground --profile personal --query TemplateBody --output text` must match the start of `docker image inspect <function>:latest --format '{{.Id}}'`.
- Don't use an inline `DOCKER_HOST=… sam build && sam deploy`. The prefix reaches only `sam build`, and on 2026-09-28 the deploy pushed a stale image from another Docker engine, which rolled the live code back.
- Never run `sam delete`. It would delete the whole stack.

## Commands

```bash
uv sync                                        # install / update all workspace deps
uv run --package <name> <entry-script>         # run a package locally
uv run --package <name> pytest packages/<name>/tests/   # test a package
uv run ruff check --fix . && uv run ruff format .       # lint + format
sam build && sam deploy --no-execute-changeset # build + change set; export DOCKER_HOST first (see SAM / deployment)
```

## Packages

### genealogy (`packages/genealogy/`)

A personal family tree built up through MCP tools and exported as GEDCOM. It is fed by WikiTree, Library of Congress newspapers, and GEDCOM files from other apps.

- **Tools:**
  - `wikitree_*` (search, get_person, get_ancestors) reads WikiTree's free public world tree.
  - `newspapers_search` does full-text search of Chronicling America (Library of Congress).
  - `tree_*` builds your own tree: add/update people, set events, link relatives, `tree_import_wikitree`, GEDCOM import (merge or replace) and export, and resource attachments.
- **Why no FamilySearch:** its API is a partner program that needs approval, and it doesn't expose record search. The client was removed and is in the repo history. For FamilySearch data, use partner desktop software (e.g. RootsMagic) and import its GEDCOM. `_FSFTID` ids still round-trip.
- **Source of truth:** `tree.json` is the canonical model in `_models.py`; `tree.ged` is a GEDCOM 5.5.1 export written by `_gedcom.py`.
- **External ids:**
  - `Person.external_ids` maps a system to an id: `wikitree`, `familysearch`, a GEDCOM `source` such as `ancestry`, or `tree:<name>`.
  - Export writes every id as a `REFN`/`TYPE` pair, including the tree's own ids under `tree:<name>`. So an export edited in another app merges back onto the same people.
- **Merging:** every source builds a `Tree`, and `Tree.merge_tree` merges it.
  - People match on any shared external id.
  - Matched people keep local edits and only gain missing data, unless `overwrite`.
  - Links go through `add_child`/`add_parent`, which never downgrade a link, steal half-siblings, or duplicate a couple.
  - GEDCOM imports from other apps need `source=` so each person gets a stable key (`_UID`, else the xref). Without it, a re-import duplicates people.
- **WikiTree (`_wikitree.py`):**
  - Calls `GET api.wikitree.com/api.php?action=...` with `appId` (`WIKITREE_APP_ID`); no auth for public profiles. Responses are a top-level array.
  - Pedigrees use `getPeople&ancestors=N`. `getAncestors` still answers but returns a deprecation message in `status`; `_call` raises on any non-empty status.
  - Dates are `YYYY-MM-DD` with zeros for unknown parts. `DataStatus` guess/before/after maps to ABT/BEF/AFT.
- **Newspapers (`_newspapers.py`):**
  - Calls `loc.gov/collections/chronicling-america/?fo=json&at=results,pagination&searchType=advanced&qs=...&ops=PHRASE|AND`.
  - `at=` cuts response time a lot; `start_date`/`end_date` only filter in advanced mode.
  - Search latency is erratic (4–50 s measured), so the 25 s timeout (`NEWSPAPER_TIMEOUT`) can be hit in Lambda.
  - Page images use IIIF `/full/pct:50/`; storage-service PDFs return 403 to scripts.
  - The `description` field is only the first ~1000 OCR characters.
- **Storage (`_storage.py`):**
  - With `TREE_BUCKET` set, files go to `s3://$TREE_BUCKET/trees/<name>/{tree.json,tree.ged,exports/,resources/}`. Otherwise they go to `$TREE_DIR` (default `~/.genealogy-mcp/trees`).
  - Saves are conditional, so overlapping invocations raise `ConcurrentModification` instead of losing writes.
  - Resources are content-addressed (`resources/<sha256[:16]>-<name>`). The bucket is versioned and retained.
- **Request size:** `MAX_REQUEST_BODY_BYTES` (4 MiB, mcp's default, set explicitly) is passed to mcp as `max_request_body_size`; larger bodies get a 413. It keeps the Lambda event, which wraps the body as an escaped JSON string, under Lambda's 6 MB cap (#505). Only inline arguments such as `tree_import_gedcom`'s `gedcom_text` count; `tree_attach_resource_from_url` downloads in the Lambda (25 MiB cap).
- **Response size:** mcp doesn't limit responses, but Lambda refuses ones over 6 MB. `tree_export_gedcom(include_text=True)` returns the GEDCOM inline only while `_inline_gedcom_bytes` (its size after the three JSON escapes on the way out: tool result, JSON-RPC body, Lambda response) is at most `MAX_INLINE_GEDCOM_BYTES` (5 MiB). Above that it returns a `note` instead, and the export is still saved; `path` (`exports/tree-<stamp>.ged`) and `url` are always returned (#509). Escaping grows quotes and backslashes 8x, 2-byte UTF-8 3x and short CRLF lines about 2.1x, which is why the cap is on the escaped size, not the raw bytes.
- **Auth:** none when deployed. When this was set up, claude.ai connectors couldn't send a static bearer header; since about Oct 2026 the custom-connector setup has a "Request headers" step, so a secret could now be turned on. The app still requires `Authorization: Bearer <secret>` if `GENEALOGY_MCP_SECRET` is set (same pattern as ynab-mcp), but `template.yaml` leaves it unset.

### markets (`packages/markets/`)

Short-horizon market analysis: quotes, volatility, price-target odds and sell-plan simulation. Stateless; no storage.

- **Tools (7):**
  - `quote` returns the current price (with pre/post-market) for a ticker.
  - `price_history` returns daily OHLCV bars.
  - `earnings_event_study` measures historical moves around earnings.
  - `implied_volatility` summarizes option-implied vol by expiry.
  - `volatility_forecast` gives σ estimates and the default σ.
  - `price_target_odds` estimates the odds of touching or finishing past a price within a horizon.
  - `evaluate_sell_plan` simulates a sell plan over simulated paths. Tranche types are `market`, `limit`, `stop_limit` and `bracket` (there is no plain stop; a bracket without `limit_price` has a stop-market leg). A price field the type doesn't use is rejected.
- **Data:** yfinance only (pinned in `pyproject.toml`). Yahoo may rate-limit or block Lambda IPs; that surfaces as a `DataSourceError` ("retry later").
- **Spot:** resolved as argument > the newest of the post- and pre-market prices that are later than the regular close > last. Newest wins because Yahoo keeps the previous evening's post price through the next pre-market. With an extended-hours spot, `evaluate_sell_plan` adds a `spot_note`: day-0 market fills are modelled at that price, but a real order fills at the next regular open. Option IVs are always inverted at the regular-session price (`quote.last`), because option quotes are regular-session.
- **Implied vol:** off-hours chains have bid=ask=0 and junk Yahoo IV, so IV falls back mid > last trade > Yahoo IV, accepted only within [0.01, 3.0].
- **Default σ:** `options_ex_event` > `options_atm` > `realized_20d`. Both simulation tools take `vol_source`: `options` (default) rescales the bootstrap to that same σ (or the `vol` argument), so lognormal and bootstrap agree; `history` keeps the 5-year history's own vol (`history_5y`).
- **Path model (`_paths.py`):**
  - Stationary block bootstrap (mean block 5 days) over 5 y of history, with the overnight/intraday split kept.
  - Highs and lows come from Brownian bridges.
  - Drift is annualized arithmetic, so 0 is a martingale.
  - Cap of 2,000,000 path-days (days x paths); larger requests are refused.
- **Sell-plan fills:** marketable orders fill at spot on day 0. Limits fill at the open on a gap-up. Stop-limits that gap below the limit keep resting. In a bracket, the stop wins within the same session. Leftover shares sell at the deadline close.
- **Cache (in-process):** history 1 h, quote 30 s, earnings 12 h, chains 5 min, rate 1 h.
- **Errors:** mcp 2.x hides the text of non-`ToolError` exceptions, so tools re-raise `ValueError`/`DataSourceError` as `ToolError`.
- **Auth:** none by default; `Authorization: Bearer <secret>` is required if `MARKETS_MCP_SECRET` is set, but `template.yaml` leaves it unset.

### vault-mcp (`packages/vault-mcp/`)

The Obsidian vault as a claude.ai custom connector, in its own SAM stack `obsidian-vault` (`deploy/template.yaml`, `deploy/samconfig.toml`). It's on demand, following the soulsearch pattern. `packages/vault-mcp/README.md` is the runbook.

- **Two halves of one package:**
  - **Lambda front end** (`proxy.py`, image from `packages/vault-mcp/Dockerfile`): an always-on stable API Gateway URL.
    - It lists the box's tools by wrapping the same functions (`_forward`), so the schemas can't drift.
    - It forwards `tools/call` to the box over HTTPS on 8443 and adds `start_vault` and `health`.
    - A call while the box is stopped runs `StartInstances` and returns a "starting, call start_vault()" `ToolError`.
  - **The box** (EC2 t4g.small) runs the real Obsidian app under Xvfb with Obsidian Sync, plus `vault-mcp` (`app.py`/`main.py`) as a uv tool.
- **Lambda to box:** the same as soulsearch.
  - No VPC. The public DNS name is cached per container and forgotten on connection errors.
  - The box makes its own CA in `install.sh` and publishes it to SSM `/obsidian-vault/ca-cert`. The Lambda pins it and checks the chain but not the hostname.
  - The bearer token (SSM SecureString `/obsidian-vault/mcp-token`) is the same at both hops. Both apps fail closed without it.
- **Readiness** (`_ready.py`): after a start, the box's tools refuse calls (`VAULT_REQUIRE_READY`) until Sync has caught up:
  - the app is running;
  - Sync's status from CLI `sync:status` (falling back to an `eval` of the plugin's internals) isn't in `VAULT_SYNC_BUSY`; busy reads `syncing`;
  - the vault has been quiet for 30 s by ctime;
  - uptime is at least 60 s.

  Once ready it latches; after 10 min it serves anyway.
- **Idle stop:** `deploy/server/bin/vault-idle-stop` (a root timer, every 5 min) powers off after `VAULT_IDLE_MINUTES` (30) with no authenticated MCP request.
  - Activity is the mtime of `last_activity`, touched by `app.py` on non-401 `/mcp` requests.
  - It waits for `/readyz` `settled`, so Claude's edits upload first, and takes a `vault-snapshot` before stopping.
  - `InstanceInitiatedShutdownBehavior: stop`. A `hold` file (set by `vault-vnc`) keeps the box up.
- **No S3 backups:** Sync history plus the devices are the backup. `vault-snapshot` is a local git repo outside the vault, committed before bulk edits and idle stops.
- **Backends:**
  - The official Obsidian CLI (`obsidian-cli`) handles search, move/rename (link rewriting), delete (to trash), tasks and creating today's daily note.
  - Everything else, and every CLI action while the app is down, uses the files directly.
  - Each result names its `backend`.
- **CLI contract is loose:**
  - The exit code is always 0, errors go to stdout as `Error: ...`, and the JSON shapes are undocumented.
  - So `_cli.py` raises on those, callers verify on disk, and anything unexpected falls back to the filesystem.
  - Before each call it checks `systemctl --user is-active obsidian.service` (`OBSIDIAN_APP_CHECK`), because the CLI would otherwise launch a second app.
- **Vault Guide rules are enforced in the write tools:**
  - New notes go to `_Inbox` with `type`/`status`/`created`, and `type`/`status`/`source` vocabulary is checked.
  - A rename adds the old title to `aliases`.
  - `updated` is never touched.
- **Frontmatter** (`_notes.py`): read with `yaml.BaseLoader`, so scalars stay text. `set_properties` rewrites only the changed keys' lines; the rest of the block stays byte-exact.
- **Safety:**
  - `Vault.resolve` keeps paths in the vault (no `..`, absolute paths or symlinks out; no writes in `.obsidian`/`.trash`/`.git`).
  - Writes are serialized (`_WRITE_LOCK`), and `write_text(expect=...)` refuses if the file changed since it was read.
  - Nothing is deleted permanently. Bulk edits are dry-run first, and every write is audit-logged.
- **Concurrent sessions:**
  - Every read and write returns a `version` (sha256 of the file); edits take `if_version`.
  - `begin_write`/`end_write` (`_locks.py`) are cooperative locks with expiry. `Vault._guard` checks them on every file write; CLI moves and deletes call `_lock_check`. The tool's `lock` argument reaches the check through the `@_locked` decorator and a ContextVar.
  - The git tools (`_git.py`) work on vault-snapshot's repo (`VAULT_GIT_DIR`, default `~/snapshots/vault.git`), with literal pathspecs.
- **Types:**
  - `_notes.typed` reads frontmatter as Obsidian does (YAML 1.2 core, dates as text), and `Note.kinds` holds each property's type.
  - The dumper quotes text that would read as a number or boolean.
  - `_check_types` refuses a value whose type no other note uses for that property.
- **`run_script`** runs `_Meta/scripts/<name>` only if its sha256 is pinned in `VAULT_SCRIPTS` (SSM `scripts`, read by `install.sh`). The pin matters because the connector itself can write that folder.
- **`query_base`** goes through CLI `base:query` (`.base` files only) and computes the view's summaries itself.
- **Batch tools** (`create_notes`, `update_notes`, `bulk_update`, `move_notes`, `import_archive`):
  - They reuse the single-note plan/apply helpers (`_new_note`, `_edit_note`, `_plan_move`/`_apply_move`).
  - Each checks every item before writing; one bad item means nothing is written.
  - Up to 200 items per call. `move_notes` stops after `_BATCH_SECONDS` and returns `remaining`, to stay inside the front end's 25 s call.
  - `import_archive` (`_archive.py`) fetches only public https hosts and bounds the download, the file count and each file's size. Applying needs the plan's `sha256`, and it never overwrites.
  - SSM `readonly` unregisters the write tools on both halves.
- **Deploy** (`deploy/deploy.sh`):
  - `infra` creates the ECR repo if needed, runs `sam build`, and creates a change set with `--no-execute-changeset` for you to review.
  - `push` starts the box if needed, builds the wheels, and runs `deploy/server/install.sh` over SSM (no SSH).
  - The AMI is pinned in `samconfig.toml`. The instance is `Retain`, and its volume `DeleteOnTermination: false`. Never run `sam delete`.

### Elden Ring (moved to soulsearch)

The Elden Ring search MCP server, its data extraction, its runbook and its OpenSearch instance now live in [trey-gonsoulin/soulsearch](https://github.com/trey-gonsoulin/soulsearch) (soulsearch#1 to soulsearch#4). Bare `#N` in soulsearch's imported history refer to this repo's issues. The instance moved to soulsearch's `soulsearch` stack as `SearchInstance` (soulsearch#3), with its pinned AMI and stack policy. `mcp-playground` keeps a stack policy, which can't be deleted; since soulsearch#3 it allows every update.

## Conventions

- Use `uv` for all dependency and run management (`uv add --package <name> <dep>`, `uv run`).
- All route handlers in FastAPI should be synchronous (`def`, not `async def`) so uvicorn dispatches them to a thread pool — blocking SDK/network calls work without async wrappers.
- MCP tools can be sync too: since mcp 2.x, a sync (`def`) tool runs on a worker thread, so a slow tool no longer blocks other requests under local uvicorn (`main.py`). Lambda serves one request per invocation either way. Under local uvicorn, concurrent first calls can race to initialise lazy module-level clients (e.g. a cached SDK client); that's harmless, since the loser's client is just dropped.
- After adding a new package to the workspace, add it to `[tool.uv.workspace]` members in the root `pyproject.toml`.
- Each package's `pyproject.toml` should declare `mangum`, `mcp`, and `fastapi` as dependencies; other packages in this workspace as `{ workspace = true }` sources.
