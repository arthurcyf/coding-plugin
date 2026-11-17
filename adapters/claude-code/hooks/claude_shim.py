#!/usr/bin/env python3
"""Claude Code adapter shim (wiring only, no policy).

Claude Code hook JSON (stdin) -> coding-plugin normalized event -> core handle()
-> Claude Code hook output (exit code, stdout JSON, stderr).

  PreToolUse  Bash                   -> pre_command
  PreToolUse  Edit|Write|NotebookEdit -> pre_edit
  PreToolUse  Read                   -> pre_read
  PostToolUse Edit|Write             -> post_edit
  Stop                               -> stop (stop_hook_active -> continuing)
  SessionStart                       -> session_start

Exit 2 + stderr blocks the tool call / the stop. Anything unexpected fails open (exit 0).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def to_event(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate a Claude Code hook payload into a normalized event (None = ignore)."""
    name = payload.get("hook_event_name")
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    base = {
        "cwd": payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
        "session_id": payload.get("session_id") or "",
    }
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if name == "PreToolUse":
        if tool == "Bash":
            return {**base, "event": "pre_command", "command": tool_input.get("command") or ""}
        if tool in EDIT_TOOLS:
            return {**base, "event": "pre_edit", "path": path}
        if tool == "Read":
            return {**base, "event": "pre_read", "path": path}
    elif name == "PostToolUse" and tool in EDIT_TOOLS:
        return {**base, "event": "post_edit", "path": path}
    elif name == "Stop":
        return {**base, "event": "stop", "continuing": bool(payload.get("stop_hook_active"))}
    elif name == "SessionStart":
        return {**base, "event": "session_start"}
    return None


def to_output(event: Dict[str, Any], result: Any) -> Tuple[int, str, str]:
    """Translate a core Result into (exit_code, stdout, stderr) for Claude Code."""
    if result.code == 2:
        return 2, "", result.message
    out: Dict[str, Any] = {}
    if event["event"] == "session_start" and result.context:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": result.context}
    if result.notice:
        out["systemMessage"] = result.notice
    return 0, (json.dumps(out) if out else ""), ""


def main() -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return 0  # not a hook payload we understand: ignore
        event = to_event(payload) if isinstance(payload, dict) else None
        if event is None:
            return 0
        if LIB not in sys.path:
            sys.path.insert(0, LIB)
        import coding_plugin

        code, out, err = to_output(event, coding_plugin.handle(event))
        if out:
            sys.stdout.write(out + "\n")
        if err:
            sys.stderr.write(err.rstrip("\n") + "\n")
        return code
    except Exception:
        if os.environ.get("CODING_PLUGIN_STRICT") == "1":
            raise
        return 0


if __name__ == "__main__":
    sys.exit(main())
