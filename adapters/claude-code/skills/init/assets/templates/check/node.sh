#!/bin/sh
# Project check (coding-plugin convention): format, lint, typecheck, tests.
# Exits non-zero on failure. Used by the agent Stop hook, git pre-push and CI.
# Runs package.json scripts if they exist; tailor the names to your project.
set -eu
cd "$(dirname "$0")/.."

PM=npm
[ -f pnpm-lock.yaml ] && PM=pnpm
[ -f yarn.lock ] && PM=yarn
[ -f bun.lockb ] && PM=bun

has_script() {
  node -e 'const s=(require("./package.json").scripts||{}); process.exit(s[process.argv[1]] ? 0 : 1)' "$1"
}
run_script() {
  if has_script "$1"; then echo "+ $PM run $1"; "$PM" run "$1"; else echo "(no '$1' script; skipped)"; fi
}

run_script format:check
run_script lint
run_script typecheck
run_script test
