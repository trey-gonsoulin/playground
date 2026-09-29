FROM public.ecr.aws/lambda/python:3.13 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Third-party dependencies only, so this layer is rebuilt (and re-pushed) when
# the lock changes, not on every source edit. The lock pins versions from a
# private index; the same versions install from PyPI.
COPY pyproject.toml uv.lock ./
COPY packages/ynab-mcp/pyproject.toml ./packages/ynab-mcp/
RUN uv export --package ynab-mcp --no-dev --no-emit-workspace --frozen --no-hashes \
        -o requirements.txt \
    && uv pip install --no-cache --index-url https://pypi.org/simple \
        --python /var/lang/bin/python3 --target /deps -r requirements.txt

FROM public.ecr.aws/lambda/python:3.13

COPY --from=builder /deps "${LAMBDA_TASK_ROOT}/"
COPY packages/ynab-mcp/ynab_mcp "${LAMBDA_TASK_ROOT}/ynab_mcp"

CMD ["ynab_mcp.lambda_handler.handler"]
