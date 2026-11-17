#!/bin/sh
# Project check (coding-plugin convention): format, lint, typecheck, tests.
# Exits non-zero on failure. Used by the agent Stop hook, git pre-push and CI.
# Tailor freely; keep it fast and deterministic.
set -eu
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
run() { echo "+ $*"; "$@"; }
has() { command -v "$1" >/dev/null 2>&1; }
need() {  # missing tools are a warning locally, a failure in CI
  if has "$1"; then return 0; fi
  if [ "${CI:-}" = "true" ]; then echo "error: $1 is required in CI" >&2; exit 1; fi
  echo "warning: $1 not installed; skipping $2" >&2
  return 1
}

if need ruff "format and lint"; then
  run ruff format --check .
  run ruff check .
fi
if [ -f mypy.ini ] || grep -q '^\[tool\.mypy\]' pyproject.toml 2>/dev/null; then
  if need mypy "typecheck"; then run mypy .; fi
fi
if has pytest; then
  run pytest -q
else
  run "$PY" -m unittest discover -q
fi
