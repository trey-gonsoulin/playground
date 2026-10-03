# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal

This repo is a collection of personal MCP servers deployed to AWS Lambda and integrated with Claude Desktop / Claude iOS. Each MCP is its own package under `packages/`, following the same structure as `~/code/hoshi` (the reference implementation). The root `pyproject.toml` is a **uv workspace** with one member per MCP package.

## Architecture pattern

Each MCP package follows this layout (see `~/code/hoshi/packages/hoshi-api/` as the reference):

```
packages/<name>/
  <name>/
    mcp_server.py      # FastMCP tools — the main thing to write
    app.py             # FastAPI factory that mounts the MCP at /mcp
    lambda_handler.py  # Mangum(app) entry point for Lambda
    main.py            # uvicorn CLI entry for local dev
  Dockerfile           # multi-stage: builder (uv sync) → final Lambda image
  pyproject.toml       # hatchling build, mangum + mcp + fastapi deps
```

### MCP server setup

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "my-mcp-name",
    stateless_http=True,   # each Lambda invocation is independent
    json_response=True,    # plain JSON, not SSE — required behind API Gateway
    streamable_http_path="/",
    host="0.0.0.0",        # avoids DNS-rebinding protection rejecting non-localhost Host headers
)
```

Mount into FastAPI at `/mcp`:

```python
application.mount("/mcp", mcp.streamable_http_app())
```

Add a path-normalization middleware to avoid a Starlette redirect on `/mcp` → `/mcp/`:

```python
async def _normalize_mcp_path(request, call_next):
    if request.url.path == "/mcp":
        request.scope["path"] = "/mcp/"
    return await call_next(request)
```

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

Build and deploy:

```bash
sam build
sam deploy          # first run: sam deploy --guided to populate samconfig.toml
```

## Commands

```bash
uv sync                                        # install / update all workspace deps
uv run --package <name> <entry-script>         # run a package locally
uv run --package <name> pytest packages/<name>/tests/   # test a package
uv run ruff check --fix . && uv run ruff format .       # lint + format
sam build && sam deploy                        # build and deploy to AWS
```

## Packages

### therapist-finder (`packages/therapist-finder/`)

Searches [inclusivetherapists.com](https://www.inclusivetherapists.com) and [psychologytoday.com](https://www.psychologytoday.com) and returns therapist profiles. One MCP tool: `search_therapists(location, specialties, insurance, telehealth, lgbtq, issue, sources, max_results)`.

**Inclusive Therapists scraping mechanics:**
- First page: `GET /{state-slug}/{city-slug}?{filter}=on` — IT uses full state names in URL slugs (`new-york`, not `ny`), mapped via `_IT_STATE_SLUGS` in `_scrapers.py`.
- Subsequent pages: `POST /wapi/widget` with multipart FormData. The `queryString` JS object embedded in each page response contains the exact parameters to mirror back for pagination.
- Filters are checkbox slugs (e.g. `anxiety=on`, `virtual-video-online-therapy-counseling-coaching-teletherapy=on`). Common human-readable terms are mapped to slugs in `_IT_SPECIALTY_SLUGS` and `_IT_INSURANCE_SLUGS`.

**Psychology Today scraping mechanics:**
- `GET /us/therapists/{state-abbr-lower}/{city-slug}?issue=X&insurance=Y&telehealth=true&lgbta=true`
- Results are server-side rendered as a Nuxt 3 flat-array state object in a `<script>` tag. Each therapist is a dict with `firstName`, `lastName`, `suffixes`, `primaryLocation`, `personalStatements`, `accepting_appointments`, `appointmentTypes`, and `urlPath` fields that reference other positions in the flat array by integer index. `_deref()` in `_scrapers.py` resolves those references. The `urlPath` template placeholders `[COUNTRY_CODE]` and `[PROFILE_CLASS]` are replaced with `us` and `therapists` respectively.

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
- **Auth:** none when deployed, like elden-ring and therapist-finder, because claude.ai connectors can't send a static bearer header. The app still requires `Authorization: Bearer <secret>` if `GENEALOGY_MCP_SECRET` is set (same pattern as ynab-mcp), but `template.yaml` leaves it unset.

## Conventions

- Use `uv` for all dependency and run management (`uv add --package <name> <dep>`, `uv run`).
- All route handlers in FastAPI should be synchronous (`def`, not `async def`) so uvicorn dispatches them to a thread pool — blocking SDK/network calls work without async wrappers.
- After adding a new package to the workspace, add it to `[tool.uv.workspace]` members in the root `pyproject.toml`.
- Each package's `pyproject.toml` should declare `mangum`, `mcp`, and `fastapi` as dependencies; other packages in this workspace as `{ workspace = true }` sources.
