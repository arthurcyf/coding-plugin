# coding-plugin

Agent-neutral guardrails that make AI coding agents more reliable in every project:
- blocks dangerous shell commands and edits to protected or secret files
- formats files on edit
- refuses to let the agent stop while the project checks fail
- ships reusable **fix-issue / feature / review / handoff / sandbox** workflows and reviewer, test-writer, investigator, security-auditor and sandbox-worker roles

Claude Code is the first supported agent (as a plugin). Enforcement also works with any agent, or with no agent at all, through git hooks and CI.

```
/plugin install coding-plugin --marketplace arthurcyf/coding-plugin
```

## What it guards against

Three independent layers enforce the same rules, so they hold even if an agent ignores its instructions, or no agent is involved at all:

| Layer | When | What it enforces |
|---|---|---|
| Agent hooks (Claude Code) | During the session | Command guard, path guard, secret-read guard, format on edit, checks before stopping, session context |
| Git hooks (`.githooks/`) | Commit and push, by any agent or human | Secret scan, protected paths, no force-push or deletion of main, checks before push |
| CI (GitHub Actions, GitLab, Bitbucket, Azure, or a step in your existing pipeline) | Every push and pull request | Full secret scan and the check command |

Specifically, it blocks:
- dangerous shell commands: `rm -rf ~` or `/`, `curl | sh`, `chmod -R 777`, force-push or `reset --hard` on a protected branch
- edits to protected paths: `.env`, `secrets/**`, `**/*.pem`, `**/*.key`, `.git/**`
- reading secret files
- declaring the task done while the project's checks (lint, typecheck, tests) are failing
- committing or pushing secrets, protected paths, or a force-push/delete of `main`

For configuration, CI setup, disabling guards, tracing and everything else, see [DOCUMENTATION.md](DOCUMENTATION.md).

## Install

### Claude Code
```
/plugin marketplace add arthurcyf/coding-plugin
/plugin install coding-plugin@coding-plugin
```
Then, inside a project, run `/coding-plugin:init`. It detects the stack and asks which agents you use. It shows a dry run and asks before overwriting anything. It then writes:
- `scripts/check`
- `AGENTS.md` and `CLAUDE.md`
- `.coding-plugin.json`
- the git hooks
- the CI workflow

Skills: `/coding-plugin:init`, `/coding-plugin:fix-issue`, `/coding-plugin:feature`, `/coding-plugin:review`, `/coding-plugin:handoff`, `/coding-plugin:sandbox`.

Subagents: `coding-plugin:reviewer`, `coding-plugin:test-writer`, `coding-plugin:investigator`, `coding-plugin:security-auditor`, `coding-plugin:sandbox-worker`.

### Any other agent, or none
Clone this repo once, then in each project:
```sh
python3 /path/to/coding-plugin/core/scripts/coding_plugin.py install --target . --stack python --agents codex --dry-run
python3 /path/to/coding-plugin/core/scripts/coding_plugin.py install --target . --stack python --agents codex
```
Stacks: `python`, `node`, `go`, `rust`, `none`. For CI options, see [DOCUMENTATION.md](DOCUMENTATION.md#ci).

This vendors the core into `.coding-plugin/bin/`, so the git hooks and CI need nothing else installed. Commit `.coding-plugin/bin/`. Logs and state in `.coding-plugin/` are ignored automatically.

| Agent | Status | How it is covered today |
|---|---|---|
| Claude Code | **Supported** | Plugin: hooks, skills, subagents. `CLAUDE.md` imports `AGENTS.md` |
| Codex CLI, Cursor, Copilot | Planned adapter | Read `AGENTS.md` natively; git hooks and CI enforce |
| Gemini CLI | Planned adapter | Set `context.fileName: ["AGENTS.md"]`; git hooks and CI enforce |

See [DOCUMENTATION.md](DOCUMENTATION.md) for architecture, configuration, CI, disabling guards, tracing, sandboxed subagent tasks, repository layout, development and troubleshooting.
