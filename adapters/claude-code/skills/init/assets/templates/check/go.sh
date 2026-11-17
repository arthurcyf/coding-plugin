#!/bin/sh
# Project check (coding-plugin convention): format, lint, typecheck, tests.
# Exits non-zero on failure. Used by the agent Stop hook, git pre-push and CI.
set -eu
cd "$(dirname "$0")/.."

run() { echo "+ $*"; "$@"; }

echo "+ gofmt -l ."
unformatted=$(gofmt -l .)
if [ -n "$unformatted" ]; then
  echo "These files need gofmt:" >&2
  echo "$unformatted" >&2
  exit 1
fi
run go vet ./...
if command -v staticcheck >/dev/null 2>&1; then run staticcheck ./...; fi
run go test ./...
