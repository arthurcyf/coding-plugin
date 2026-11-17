#!/bin/sh
# Project check (coding-plugin convention): format, lint, typecheck, tests.
# Exits non-zero on failure. Used by the agent Stop hook, git pre-push and CI.
set -eu
cd "$(dirname "$0")/.."

run() { echo "+ $*"; "$@"; }

run cargo fmt --all -- --check
run cargo clippy --all-targets --all-features -- -D warnings
run cargo test --all-features
