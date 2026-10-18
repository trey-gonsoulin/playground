#!/usr/bin/env bash
# Quality gate for the elden-ring package (#457): lint, format, types, then the offline
# tests with a coverage floor. There is no CI, so run it before pushing.
# The lint tools are in the `lint` group; after the first online run they are cached and
# `--offline` works too. Extra arguments go to `uv run`.
set -euo pipefail
cd "$(dirname "$0")/.."
run=(uv run --frozen --package elden-ring --group lint "$@")
"${run[@]}" ruff check .
"${run[@]}" ruff format --check .
"${run[@]}" pyright
"${run[@]}" pytest --cov=elden_ring --cov=scripts --cov-fail-under=45
