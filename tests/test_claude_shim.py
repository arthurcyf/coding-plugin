"""Claude Code adapter: real hook payloads -> normalized events -> Claude hook output."""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

from helpers import ROOT, RepoTestCase, cp, load_fixture

HOOKS = os.path.join(ROOT, "adapters", "claude-code", "hooks")
SHIM = os.path.join(HOOKS, "claude_shim.py")

spec = importlib.util.spec_from_file_location("claude_shim", SHIM)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def substitute(obj, repo):
    if isinstance(obj, dict):
        return {k: substitute(v, repo) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, repo) for v in obj]
    if isinstance(obj, str):
        return obj.replace("{repo}", repo)
    return obj


class ShimMapping(RepoTestCase):
    def payload(self, name):
        return substitute(load_fixture("claude", "payloads.json")[name], self.repo)

    def test_to_event(self):
        cases = {
            "pre_bash_dangerous": {"event": "pre_command", "command": "rm -rf ~"},
            "pre_edit_env": {"event": "pre_edit", "path": os.path.join(self.repo, ".env").replace("\\", "/")},
            "pre_notebook_secret": {"event": "pre_edit"},
            "pre_read_env": {"event": "pre_read"},
            "post_edit_txt": {"event": "post_edit"},
            "stop_first": {"event": "stop", "continuing": False},
            "stop_continuing": {"event": "stop", "continuing": True},
            "session_start": {"event": "session_start", "session_id": "sess-1"},
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                event = shim.to_event(self.payload(name))
                self.assertEqual(event["cwd"], self.repo)
                for key, value in expected.items():
                    got = event[key].replace("\\", "/") if isinstance(event[key], str) else event[key]
                    self.assertEqual(got, value)
        self.assertIsNone(shim.to_event(self.payload("pre_grep_ignored")))
        self.assertIsNone(shim.to_event({"hook_event_name": "Notification"}))

    def test_to_output(self):
        ev = {"event": "pre_command"}
        self.assertEqual(shim.to_output(ev, cp.Result(2, "no")), (2, "", "no"))
        self.assertEqual(shim.to_output(ev, cp.Result()), (0, "", ""))
        code, out, _ = shim.to_output({"event": "session_start"}, cp.Result(context="ctx"))
        self.assertEqual(json.loads(out)["hookSpecificOutput"],
                         {"hookEventName": "SessionStart", "additionalContext": "ctx"})
        code, out, _ = shim.to_output({"event": "stop"}, cp.Result(notice="gave up"))
        self.assertEqual(json.loads(out), {"systemMessage": "gave up"})


class ShimEndToEnd(RepoTestCase):
    """Runs the shim as Claude Code would: payload on stdin, exit code + streams out."""

    def payload(self, name):
        return json.dumps(substitute(load_fixture("claude", "payloads.json")[name], self.repo))

    def run_shim(self, name, via_launcher=False):
        argv = [cp.find_sh(), os.path.join(HOOKS, "run")] if via_launcher else [sys.executable, SHIM]
        return subprocess.run(argv, input=self.payload(name), capture_output=True, text=True,
                              encoding="utf-8", cwd=self.repo)

    def test_guards(self):
        expectations = {
            "pre_bash_dangerous": 2, "pre_bash_safe": 0, "pre_edit_env": 2, "pre_write_src": 0,
            "pre_notebook_secret": 2, "pre_read_env": 2, "pre_grep_ignored": 0,
        }
        for name, code in expectations.items():
            with self.subTest(name=name):
                p = self.run_shim(name)
                self.assertEqual(p.returncode, code, p.stderr)
                if code == 2:
                    self.assertIn("coding-plugin blocked", p.stderr)

    def test_session_start_context(self):
        p = self.run_shim("session_start")
        self.assertEqual(p.returncode, 0, p.stderr)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("WARNING: you are on protected branch 'main'", ctx)

    def test_stop_blocks_then_gives_up(self):
        self.write("scripts/check.py", "import sys\nprint('boom')\nsys.exit(1)\n")
        self.write(".coding-plugin.json", json.dumps({"stopRetryCap": 1}))
        p = self.run_shim("stop_first")
        self.assertEqual(p.returncode, 2)
        self.assertIn("boom", p.stderr)
        p = self.run_shim("stop_continuing")
        self.assertEqual(p.returncode, 0)
        self.assertIn("still failing", json.loads(p.stdout)["systemMessage"])

    def test_post_edit_formats(self):
        fmt = self.write("tools/upper.py", "import sys\np=sys.argv[1]\ns=open(p).read()\nopen(p,'w').write(s.upper())\n")
        self.write(".coding-plugin.json", json.dumps({"formatters": {".txt": [sys.executable, fmt, "{file}"]}}))
        self.write("notes.txt", "abc\n")
        self.assertEqual(self.run_shim("post_edit_txt").returncode, 0)
        self.assertEqual(self.read("notes.txt"), "ABC\n")

    def test_garbage_input_fails_open(self):
        for raw in ("", "not json", "[1, 2]", '{"hook_event_name": "PreToolUse"}'):
            with self.subTest(raw=raw):
                p = subprocess.run([sys.executable, SHIM], input=raw, capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(cp.find_sh(), "sh not available")
    def test_launcher(self):
        self.assertEqual(self.run_shim("pre_bash_dangerous", via_launcher=True).returncode, 2)
        self.assertEqual(self.run_shim("pre_bash_safe", via_launcher=True).returncode, 0)

    def test_trace_picks_up_session_id_with_no_shim_change(self):
        self.write(".coding-plugin.json", json.dumps({"trace": {"enabled": True}}))
        self.assertEqual(self.run_shim("pre_bash_safe").returncode, 0)
        trace_path = os.path.join(self.repo, ".coding-plugin", "log", "trace.jsonl")
        with open(trace_path, encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertEqual(rec["layer"], "claude-code")


if __name__ == "__main__":
    unittest.main()
