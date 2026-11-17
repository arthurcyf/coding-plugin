# Adapters

An adapter maps the agent-neutral core onto one agent's native mechanisms. **Adapters contain wiring only**: no policy and no business logic. Every decision is made by `core/scripts/coding_plugin.py`, and every piece of content comes from `core/skills`, `core/roles` and `core/templates`.

## The contract

### 1. Normalized event (adapter → core)
```json
{"event": "pre_command | pre_edit | pre_read | post_edit | stop | session_start",
 "cwd": "/abs/path", "command": "...", "path": "...",
 "session_id": "...", "continuing": false}
```
- `command` is only used by `pre_command`, and `path` by the edit and read events.
- `continuing` is true when the agent is already continuing because a stop was blocked (Claude Code's `stop_hook_active`).

Call `coding_plugin.handle(event)` in-process (import from the adapter's bundled `lib/`), or pipe the event to `coding_plugin.py hook`.

### 2. Result (core → adapter)

| Field | Meaning | Typical mapping |
|---|---|---|
| `code` | `0` allow/pass, `2` deny/block | Exit code, or the agent's deny / `decision: block` JSON |
| `message` | Blocking reason, written for the agent | stderr, or the agent's "reason" field |
| `context` | Extra context (session start) | stdout, or `additionalContext` |
| `notice` | Non-blocking note for the user | `systemMessage` or similar |

`handle()` never raises; it fails open. A shim must also never raise. This is critical for agents whose hooks **fail closed** on errors (Copilot).

### 3. Generator
Add `gen_<agent>(stamped_core, version) -> {path: text}` to `build/generate.py` and register it in `ADAPTERS`. It produces:
- the core copy (`lib/coding_plugin.py` + `lib/rules/`) if the agent can't reach the repo at runtime
- skills: copy `core/skills/*`, replace `{{delegate:<role>}}` and `{{skill_dir}}` with the agent's wording, add agent-specific frontmatter, bundle the referenced roles into `references/`, and bundle the installer for `init`
- roles: render `core/roles/*.md` into the agent's custom-agent format, mapping the abstract permissions (`read`, `run`, `edit`) onto its tools
- packaging manifests (plugin, extension or marketplace files)

Hand-written files (hook wiring, shim, launcher) live in `adapters/<agent>/hooks/`.

### 4. Tests
Add real payload fixtures to `tests/fixtures/<agent>/` and a `tests/test_<agent>_shim.py` that covers:
- payload → normalized event
- Result → agent output
- an end-to-end run of the shim with stdin
- garbage input failing open

## Status and designs

### Claude Code: built (`adapters/claude-code/`)
- **Instructions:** `CLAUDE.md` containing `@AGENTS.md`. Claude Code reads AGENTS.md natively only when no CLAUDE.md exists, so the import is the reliable path.
- **Skills:** plugin `skills/`, namespaced as `/coding-plugin:<skill>`.
- **Roles:** plugin `agents/*.md`, with `tools` mapped from the role permissions.
- **Hooks:** `hooks/hooks.json` → `sh hooks/run` → `claude_shim.py`. Exit 2 blocks, and stderr is fed to Claude.
- **Packaging:** the plugin is copied into a cache on install, so the generated `lib/` holds its own core copy. The marketplace is `.claude-plugin/marketplace.json` at the repo root, with `"source": "./adapters/claude-code"`.

### Shared skills output (planned)
Codex CLI, Cursor, Gemini CLI and Copilot all load Agent Skills from `.agents/skills/` (project) and `~/.agents/skills/` (user). One generic output covers all four: `adapters/agents-skills/coding-plugin-<skill>/`.
- The skills are prefixed because these agents don't namespace skills, and `init`/`review` collide with built-ins.
- Delegation reads: "If your agent supports subagents, delegate to a subagent using `references/<role>.md`; otherwise follow it yourself."

### Per-agent designs (verified against vendor docs, 2026-11-17)

| | Codex CLI | Cursor | Gemini CLI | GitHub Copilot |
|---|---|---|---|---|
| Instructions | AGENTS.md (native, nested) | AGENTS.md (native) | `.gemini/settings.json` → `{"context": {"fileName": ["AGENTS.md"]}}` (merge, never overwrite) | AGENTS.md (native, nearest wins) |
| Skills | `.agents/skills` | `.agents/skills` | `.agents/skills` | `.agents/skills` |
| Roles | `.codex/agents/<role>.toml` (`name`, `description`, `developer_instructions`) | `.cursor/agents/<role>.md` (`readonly: true` for read-only roles) | `.gemini/agents/<role>.md` (`tools` list) | `.github/agents/<role>.agent.md` (`tools`) |
| Hooks file | `.codex/hooks.json` (user must trust it via `/hooks`) | `.cursor/hooks.json` | `hooks` in `.gemini/settings.json` | `.github/hooks/coding-plugin.json` |
| Event mapping | `PreToolUse`/`PostToolUse`/`Stop`/`SessionStart`; payload ≈ Claude's, so the shim is nearly identical | `beforeShellExecution` → pre_command, `afterFileEdit` → post_edit, `beforeReadFile` → pre_read, `stop` (+`loop_limit`), `sessionStart` | `BeforeTool` (`run_shell_command` → pre_command, `write_file\|replace` → pre_edit), `AfterTool`, `AfterAgent` → stop, `SessionStart` | Use PascalCase event names (`PreToolUse` etc.) to get Claude-shaped payloads; `agentStop` → stop |
| Deny | exit 2 or `permissionDecision: "deny"` | `{"permission": "deny", "agent_message": ...}` or exit 2 | exit 2 or `{"decision": "deny", "reason": ...}` | `permissionDecision: "deny"` or exit 2. **Errors fail closed** |
| Packaging | Codex plugin (root `plugin.json`) | Cursor plugin (`.cursor-plugin/plugin.json`) | Gemini extension (`gemini-extension.json`) | Agent Plugin (`plugin.json`) |

Suggested order: Codex (cheapest, with Claude-compatible hooks), then Copilot, then Cursor, then Gemini. Reassess Gemini first: since June 2026, Antigravity CLI has replaced Gemini CLI for free and Google One users.

Agents without usable hooks are still covered by the git hooks and CI. The skills also end with "run the check command; don't claim done until it passes".

### Why there is no MCP server
Every target agent has hooks. MCP tools run only when the model decides to call them, so they can't enforce anything. A server is also a long-running process with protocol code to maintain. Git hooks and CI already cover agents without hooks.
