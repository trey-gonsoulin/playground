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

## Conventions

- Use `uv` for all dependency and run management (`uv add --package <name> <dep>`, `uv run`).
- All route handlers in FastAPI should be synchronous (`def`, not `async def`) so uvicorn dispatches them to a thread pool — blocking SDK/network calls work without async wrappers.
- After adding a new package to the workspace, add it to `[tool.uv.workspace]` members in the root `pyproject.toml`.
- Each package's `pyproject.toml` should declare `mangum`, `mcp`, and `fastapi` as dependencies; other packages in this workspace as `{ workspace = true }` sources.
