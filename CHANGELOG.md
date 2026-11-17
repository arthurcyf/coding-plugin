# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-17

### Added
- Core CLI `core/scripts/coding_plugin.py` (Python 3.9+, stdlib only, macOS/Linux/Windows):
  - Guards: `guard-command` (data-driven rules in `core/rules/commands.json`), `guard-path` (edit and read modes)
  - Formatting and checks: `format-file`, `run-checks` (change detection, retry cap, loop guard)
  - Session and secrets: `session-context`, `scan-secrets` (built-in patterns, plus gitleaks when installed)
  - Git hooks and CI: `pre-commit`, `pre-push`, `ci`
  - Setup and adapters: `install` (vendoring and templates, never overwrites silently), and the normalized `hook` entry point
- Git hook and GitHub Actions templates, plus `scripts/check` examples for Python, Node, Go and Rust.
- Claude Code adapter: a plugin and repo-root marketplace containing:
  - hooks for SessionStart, PreToolUse (Bash, Edit/Write/NotebookEdit, Read), PostToolUse and Stop
  - generated subagents and skills
- Agent-neutral skills: `init`, `fix-issue`, `feature`, `review`, `handoff`.
- Roles: `reviewer`, `test-writer`, `investigator`, `security-auditor`.
- `build/generate.py`, with a `--check` sync mode enforced in CI.
