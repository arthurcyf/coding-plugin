# coding-plugin

Agent-neutral guardrails that make AI coding agents more reliable in every project:
- blocks dangerous shell commands and edits to protected or secret files
- formats files on edit
- refuses to let the agent stop while the project checks fail
- ships reusable **fix-issue / feature / review / handoff** workflows and reviewer, test-writer, investigator and security-auditor roles

Claude Code is the first supported agent (as a plugin). Enforcement also works with any agent, or with no agent at all, through git hooks and CI.

```
/plugin install coding-plugin --marketplace arthurcyf/coding-plugin
```

## Concept

```
              core/  (all logic and content, written once)
   scripts/coding_plugin.py · rules/*.json · skills/ · roles/ · templates/
                    │
      build/generate.py  (generates adapters; CI fails if they drift)
                    │
   adapters/claude-code  (plugin: hooks + shim, agents, skills)    adapters/<next agent> ...
```

**Three enforcement layers, one implementation.** Each layer calls the same `coding_plugin.py`:

| Layer | When | What it enforces |
|---|---|---|
| Agent hooks (Claude Code) | During the session | Command guard, path guard, secret-read guard, format on edit, checks before stopping, session context |
| Git hooks (`.githooks/`) | Commit and push, by any agent or human | Secret scan, protected paths, no force-push or deletion of main, checks before push |
| CI (GitHub Actions, GitLab, Bitbucket, Azure, or a step in your existing pipeline) | Every push and pull request | Full secret scan and the check command |

## Quick start

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

Skills: `/coding-plugin:init`, `/coding-plugin:fix-issue`, `/coding-plugin:feature`, `/coding-plugin:review`, `/coding-plugin:handoff`.

Subagents: `coding-plugin:reviewer`, `coding-plugin:test-writer`, `coding-plugin:investigator`, `coding-plugin:security-auditor`.

### Any other agent, or none
Clone this repo once, then in each project:
```sh
python3 /path/to/coding-plugin/core/scripts/coding_plugin.py install --target . --stack python --agents codex --dry-run
python3 /path/to/coding-plugin/core/scripts/coding_plugin.py install --target . --stack python --agents codex
```
Stacks: `python`, `node`, `go`, `rust`, `none`. For CI, see [CI](#ci) below.

This vendors the core into `.coding-plugin/bin/`, so the git hooks and CI need nothing else installed. Commit `.coding-plugin/bin/`. Logs and state in `.coding-plugin/` are ignored automatically.

| Agent | Status | How it is covered today |
|---|---|---|
| Claude Code | **Supported** | Plugin: hooks, skills, subagents. `CLAUDE.md` imports `AGENTS.md` |
| Codex CLI, Cursor, Copilot | Planned adapter | Read `AGENTS.md` natively; git hooks and CI enforce |
| Gemini CLI | Planned adapter | Set `context.fileName: ["AGENTS.md"]`; git hooks and CI enforce |

The per-agent designs are in [adapters/README.md](adapters/README.md).

## Project conventions

- **`scripts/check`** (or a Makefile `check` target, or `checkCommand` in the config) runs format check, lint, typecheck and tests, and exits non-zero on failure. The toolkit never hard-codes a language's tools; it only runs this command.
- **`AGENTS.md`** is the single source of project instructions. Agent-specific files only point to it. For example, `CLAUDE.md` contains `@AGENTS.md` plus any Claude-only notes.
- **`.coding-plugin.json`** at the repo root holds the per-project config. Every key is optional.

## Configuration (`.coding-plugin.json`)

| Key | Default | Meaning |
|---|---|---|
| `checkCommand` | auto | Command to run. Auto order: `scripts/check`, `scripts/check.sh`, `scripts/check.py`, then `make check` |
| `checkTimeoutSec` | `600` | A check that runs longer is killed. A timeout never blocks |
| `requireChecksOnStop` | `true` | Run checks when the agent tries to stop |
| `stopRetryCap` | `3` | How many times in a row a stop can be blocked before the agent is let go with a warning |
| `preCommitCheck` | `false` | Also run the check command in pre-commit (pre-push always runs it) |
| `formatOnEdit` | `true` | Format files after the agent edits them |
| `formatters` | `{}` | Per-extension override, e.g. `{".py": ["ruff", "format", "{file}"]}`, or a list of such commands to try in order |
| `protectedPaths` | `.env`, `.env.*` (except examples), `secrets/**`, `**/*.pem`, `**/*.key`, `.git/**` | Paths the agent may not edit and commits may not touch. Setting this replaces the defaults |
| `secretPaths` | `rules/secrets.json` `secretFiles` | Files the agent may not read, and that must not be committed |
| `protectedBranches` | `["main", "master"]` | No force-push, deletion or `reset --hard` on these branches; session warning when on one |
| `commandRules` | `{"extraDeny": [], "allow": []}` | Add deny rules (`{"id", "pattern", "message"}`), or allow-list regexes that skip all command rules |
| `disabledGuards` | `[]` | Guard names or rule ids to turn off (see below) |

**Patterns** follow gitignore style:
- A pattern without `/` matches any path component.
- A pattern with `/` is anchored to the repo root.
- `**` crosses directories.
- `!` negates, and later patterns win.
- Matching is case-insensitive.

**Default formatters** are used only when installed, and a missing formatter is never an error:
- ruff, or black (for Python)
- prettier, but only from the project's `node_modules/.bin` (never via npx)
- gofmt
- rustfmt

### CI
CI runs a single command, so it works on any CI system:
```
python3 .coding-plugin/bin/coding_plugin.py ci     # full secret scan + the check command
```
`install --ci <choice>` (and `/coding-plugin:init`) decides what to generate:

| `--ci` | Result |
|---|---|
| `auto` (default) | **If a pipeline already exists** (GitHub workflows, GitLab, Bitbucket, Azure, Jenkins, CircleCI), it creates nothing and prints the one step to add to it. **Otherwise** it picks the system from the git remote, defaulting to GitHub. |
| `github` | `.github/workflows/coding-plugin.yml`, a separate workflow that runs next to your others |
| `gitlab` / `bitbucket` / `azure` | `.gitlab-ci.yml` / `bitbucket-pipelines.yml` / `azure-pipelines.yml`, only if that file doesn't exist yet |
| `none` | No CI file |

**Existing pipelines are never modified or replaced** unless you explicitly pass `--overwrite <file>`. The init skill offers to add the step to your pipeline for you, and shows the diff before editing. If your pipeline already runs the tests, add only `coding_plugin.py scan-secrets --all`, so the tests don't run twice.

### Disabling guards
- **Permanently:** `"disabledGuards": ["read-secrets", "pipe-to-shell"]`.
- **For one session or command:** `CODING_PLUGIN_DISABLE=run-checks git push` (comma-separated, or `all`).

Guard names:
- `guard-command`
- `guard-path`
- `read-secrets`
- `format-file`
- `run-checks`
- `secret-scan`
- `force-push`

Command rule ids are listed in [core/rules/commands.json](core/rules/commands.json):
- `rm-recursive-dangerous`
- `git-force-push-protected`
- `git-reset-hard-protected`
- `pipe-to-shell`
- `shell-eval-download`
- `chmod-777-recursive`
- `read-secret-file`

For a false positive in the secret scan, add `coding-plugin:allow-secret` in a comment on that line.

## Behaviour notes

- **Fails open.** A bug or crash in the toolkit is logged to `.coding-plugin/log/coding-plugin.log` and never blocks your work. The only things that block are the deliberate decisions: a deny or a failed check.
- **Stop gate.**
  - Checks run only if the working tree changed since the session started, or since the last green run.
  - The agent gets the trimmed tail of the failure output.
  - After `stopRetryCap` consecutive failures the agent is allowed to stop, and you see a warning.
  - A timeout or a missing check command never blocks.
- **Guards are a seatbelt, not a sandbox.** The command rules catch common accidents (`rm -rf ~`, `curl | sh`, force-push to main, `cat .env`), but a determined agent can get around regex rules with scripts or encoding. Use your agent's permission system and OS sandboxing for real isolation.

## Repository layout

| Path | Purpose |
|---|---|
| `core/scripts/coding_plugin.py` | The whole core: a single-file stdlib CLI with every guard, check and installer |
| `core/rules/` | Command deny rules and secret patterns (JSON, extensible) |
| `core/skills/`, `core/roles/` | Agent-neutral skills (Agent Skills `SKILL.md`) and role prompts |
| `core/templates/` | AGENTS.md / CLAUDE.md skeletons, `scripts/check` per stack, git hooks, CI workflow, starter config |
| `adapters/claude-code/` | The Claude Code plugin. `hooks/` is hand-written wiring; `lib/`, `agents/`, `skills/` are generated |
| `.claude-plugin/marketplace.json` | Makes this repo a Claude Code marketplace (generated) |
| `build/generate.py` | Generates the adapters from core; `--check` verifies they are in sync |
| `tests/` | Unit and end-to-end tests (`python3 -m unittest discover -s tests -t tests`) |
| `examples/sample-python/` | Small project for manual end-to-end testing |

## Development

```sh
python3 -m unittest discover -s tests -t tests   # 110+ tests, stdlib only
python3 build/generate.py                       # after editing anything in core/
python3 build/generate.py --check               # what CI runs
claude plugin validate adapters/claude-code     # optional: Claude Code manifest validation
```
- Try the plugin locally: `claude --plugin-dir ./adapters/claude-code`, or `/plugin marketplace add ./` from this repo.
- Never edit generated files; they are listed in `build/generated-files.txt`.
- **Releases:** bump `VERSION` (semver), add a `CHANGELOG.md` entry, run `build/generate.py`, then commit. Installed plugins update only when the version changes.

### Adding an adapter
See [adapters/README.md](adapters/README.md). In short:
- add a `gen_<agent>()` function to `build/generate.py`
- write a shim that maps the agent's hook payload to the normalized event
- add payload fixtures and a shim test
- add a README row

## Troubleshooting

- **Hooks do nothing.**
  - Check that Python 3.9+ is on PATH. On Windows, the `py` launcher is preferred.
  - Look at `.coding-plugin/log/coding-plugin.log`.
  - In Claude Code, run `/hooks` to see the registered hooks.
- **The agent keeps getting blocked at stop.** Run the check command yourself. Raise `stopRetryCap`, set `requireChecksOnStop: false`, or use `CODING_PLUGIN_DISABLE=run-checks`.
- **Git hooks don't run.**
  - `git config core.hooksPath` should print `.githooks`.
  - On macOS/Linux, the hook files must be executable (`chmod +x .githooks/*`).
- **A formatter rewrote a file the agent was editing.** Set `formatOnEdit: false`, or override `formatters` for that extension.
- **The session notes a version mismatch.** The project's vendored core is older than the plugin. Re-run `/coding-plugin:init`, or `install`; managed files update in place.

## License
MIT
