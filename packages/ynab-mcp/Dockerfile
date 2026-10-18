# Pinned by version and digest so a build can't drift without a commit. To bump,
# take the multi-arch index digest of a dated tag (both stages share it):
#   docker buildx imagetools inspect public.ecr.aws/lambda/python:<3.13.YYYY.MM.DD.NN>
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
ARG BASE_IMAGE=public.ecr.aws/lambda/python:3.13.2026.10.15.13@sha256:d5d5c74e3639a01fac254c9334a60604aa50327aa122b3ec26151e055a06a6a0

FROM ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 AS uv

FROM ${BASE_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /build

# Third-party dependencies only, so this layer is rebuilt (and re-pushed) when
# the lock changes, not on every source edit. The export carries the lock's
# sha256 hashes and the install refuses anything that doesn't match them.
COPY pyproject.toml uv.lock ./
COPY packages/ynab-mcp/pyproject.toml ./packages/ynab-mcp/
RUN uv export --package ynab-mcp --no-dev --no-emit-workspace --frozen \
        -o requirements.txt \
    && uv pip install --no-cache --require-hashes --compile-bytecode \
        --index-url https://pypi.org/simple --python /var/lang/bin/python3 --target /deps -r requirements.txt

FROM ${BASE_IMAGE}

COPY --from=builder /deps "${LAMBDA_TASK_ROOT}/"
COPY packages/ynab-mcp/ynab_mcp "${LAMBDA_TASK_ROOT}/ynab_mcp"
# /var/task is read-only at runtime and .dockerignore drops __pycache__, so
# compile here; otherwise every cold start compiles the package from source.
RUN python -m compileall -q "${LAMBDA_TASK_ROOT}/ynab_mcp"

CMD ["ynab_mcp.lambda_handler.handler"]
