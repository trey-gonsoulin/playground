FROM public.ecr.aws/lambda/python:3.13 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Third-party dependencies only, so this layer is rebuilt (and re-pushed) when
# the lock changes, not on every source edit. The lock pins versions from a
# private index; the same versions install from PyPI.
COPY pyproject.toml uv.lock ./
COPY packages/therapist-finder/pyproject.toml ./packages/therapist-finder/
RUN uv export --package therapist-finder --no-dev --no-emit-workspace --frozen --no-hashes \
        -o requirements.txt \
    && uv pip install --no-cache --index-url https://pypi.org/simple \
        --python /var/lang/bin/python3 --target /deps -r requirements.txt

FROM public.ecr.aws/lambda/python:3.13

COPY --from=builder /deps "${LAMBDA_TASK_ROOT}/"
COPY packages/therapist-finder/therapist_finder "${LAMBDA_TASK_ROOT}/therapist_finder"

CMD ["therapist_finder.lambda_handler.handler"]
