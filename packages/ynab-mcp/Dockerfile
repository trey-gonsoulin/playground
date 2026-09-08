FROM public.ecr.aws/lambda/python:3.13 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

COPY pyproject.toml uv.lock* ./
COPY packages/ynab-mcp/ ./packages/ynab-mcp/

RUN UV_INDEX_URL=https://pypi.org/simple \
    uv sync --package ynab-mcp --no-dev --no-editable

FROM public.ecr.aws/lambda/python:3.13

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

COPY pyproject.toml uv.lock* ./
COPY packages/ynab-mcp/ ./packages/ynab-mcp/

RUN UV_INDEX_URL=https://pypi.org/simple \
    uv sync --package ynab-mcp --no-dev --no-editable

RUN cp -r /build/.venv/lib/python3.13/site-packages/. "${LAMBDA_TASK_ROOT}/"

CMD ["ynab_mcp.lambda_handler.handler"]
