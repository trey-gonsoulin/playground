# The Lambda front end (vault_mcp.proxy). The box itself runs the same package
# as a uv tool, installed by deploy/server/install.sh.
#
# Pinned by version and digest so a build can't drift without a commit. To bump,
# take the multi-arch index digest of a dated tag (both stages share it):
#   docker buildx imagetools inspect public.ecr.aws/lambda/python:<3.13.YYYY.MM.DD.NN>
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
ARG BASE_IMAGE=public.ecr.aws/lambda/python:3.13.2026.10.15.13@sha256:d5d5c74e3639a01fac254c9334a60604aa50327aa122b3ec26151e055a06a6a0

FROM ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 AS uv

FROM ${BASE_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /build

# Third-party dependencies only, so this layer is rebuilt when the lock changes,
# not on every source edit. The install refuses anything that doesn't match the
# lock's hashes.
COPY pyproject.toml uv.lock ./
COPY packages/vault-mcp/pyproject.toml ./packages/vault-mcp/
RUN uv export --package vault-mcp --no-dev --no-emit-workspace --frozen \
        -o requirements.txt \
    && uv pip install --no-cache --require-hashes --compile-bytecode \
        --index-url https://pypi.org/simple --python /var/lang/bin/python3 --target /deps -r requirements.txt

FROM ${BASE_IMAGE}

COPY --from=builder /deps "${LAMBDA_TASK_ROOT}/"
# --no-emit-workspace leaves workspace members out of requirements.txt (#504).
COPY packages/mcp-lambda/mcp_lambda "${LAMBDA_TASK_ROOT}/mcp_lambda"
COPY packages/vault-mcp/vault_mcp "${LAMBDA_TASK_ROOT}/vault_mcp"
RUN python -m compileall -q "${LAMBDA_TASK_ROOT}/vault_mcp" "${LAMBDA_TASK_ROOT}/mcp_lambda"

CMD ["vault_mcp.lambda_handler.handler"]
