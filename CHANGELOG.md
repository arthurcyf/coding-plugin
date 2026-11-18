# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-18

### Added
- `/coding-plugin:sandbox <task description>` skill and `coding-plugin:sandbox-worker` subagent: runs a task in a disposable git worktree and branch (`sandbox/<slug>`), so the work never touches the caller's working tree until reviewed.
  - Isolation is a git worktree by default (zero new dependencies); the skill may additionally use a container when Docker is available and the task calls for it.
  - Nothing is merged, pushed or opened as a PR automatically -- the skill reports the worktree path and the exact review/merge/discard commands.
  - No new MCP server or core script changes: this is a plain skill + role, in keeping with the project's existing agent-neutral, hooks-only enforcement design.

## [0.3.0] - 2026-11-18

### Added
- Execution tracing: off by default, enabled with `"trace": {"enabled": true}` in `.coding-plugin.json` or `CODING_PLUGIN_TRACE=1`.
  - One JSON record per guard decision, appended to `.coding-plugin/log/trace.jsonl` (rotated at `trace.maxBytes`, default 1MB).
  - Records a `trace_id` per invocation (shared by nested calls, e.g. `pre-commit`'s own `scan_secrets` call) and the Claude Code `session_id` when available; never contains full command text, file contents, matched secret values or check output.
  - `coding_plugin.py trace tail [--lines N]` and `coding_plugin.py trace show --trace-id <id>|--session-id <id>` to inspect it.

## [0.2.0] - 2026-11-17

### Added
- `install --ci auto|github|gitlab|bitbucket|azure|none`, with templates for GitLab CI, Bitbucket Pipelines and Azure Pipelines (per-stack images and setup).
- Detection of existing CI (GitHub workflows, GitLab, Bitbucket, Azure, Jenkins, CircleCI):
  - In `auto` mode, no pipeline is generated; instead the exact step to add to the existing pipeline is printed.
  - Existing pipeline files are never offered for overwrite.
- The `init` skill asks how to handle CI: add a step to the existing pipeline (edited only after the user approves the diff), add a separate pipeline, or skip.

### Fixed
- This repo's CI: Python 3.9 is no longer available from `setup-python` on `ubuntu-latest` (Ubuntu 26.04), so the 3.9 tests now run in the official `python:3.9-bookworm` container.

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
