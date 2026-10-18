FROM public.ecr.aws/lambda/python:3.13 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Third-party dependencies only, so this layer is rebuilt (and re-pushed) when
# the lock changes, not on every source edit. The export carries the lock's
# sha256 hashes and the install refuses anything that doesn't match them.
COPY pyproject.toml uv.lock ./
COPY packages/genealogy/pyproject.toml ./packages/genealogy/
RUN uv export --package genealogy --no-dev --no-emit-workspace --frozen \
        -o requirements.txt \
    && uv pip install --no-cache --require-hashes --index-url https://pypi.org/simple \
        --python /var/lang/bin/python3 --target /deps -r requirements.txt

FROM public.ecr.aws/lambda/python:3.13

COPY --from=builder /deps "${LAMBDA_TASK_ROOT}/"
COPY packages/genealogy/genealogy "${LAMBDA_TASK_ROOT}/genealogy"

CMD ["genealogy.lambda_handler.handler"]
