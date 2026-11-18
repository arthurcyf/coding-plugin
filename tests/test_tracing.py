import json
import os
import unittest
from unittest import mock

from helpers import RepoTestCase, cp, run_git

# Built at runtime so this repository never contains a literal secret-shaped string.
FAKE_AWS = "AKIA" + "Z7Q2" * 4


class Tracing(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._had_trace_env = os.environ.pop("CODING_PLUGIN_TRACE", None)
        self.addCleanup(self._restore_trace_env)

    def _restore_trace_env(self) -> None:
        if self._had_trace_env is not None:
            os.environ["CODING_PLUGIN_TRACE"] = self._had_trace_env

    def stage(self, rel: str, content: str) -> None:
        self.write(rel, content)
        run_git(self.repo, "add", rel)

    def trace_path(self) -> str:
        return os.path.join(self.repo, ".coding-plugin", "log", "trace.jsonl")

    def read_trace(self):
        with open(self.trace_path(), encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def test_disabled_by_default(self):
        cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        self.assertFalse(os.path.exists(self.trace_path()))

    def test_enabled_via_config(self):
        self.config(trace={"enabled": True})
        res = cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        self.assertEqual(res.code, 0)
        records = self.read_trace()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["span"], "guard_command")
        self.assertEqual(rec["layer"], "claude-code")
        self.assertEqual(rec["outcome"], "allow")
        self.assertEqual(len(rec["trace_id"]), 8)
        self.assertIsNone(rec["parent_id"])
        self.assertGreaterEqual(rec["duration_ms"], 0)

    def test_enabled_via_env(self):
        os.environ["CODING_PLUGIN_TRACE"] = "1"
        cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        self.assertTrue(os.path.exists(self.trace_path()))

    def test_redacts_command_and_records_rule_id(self):
        self.config(trace={"enabled": True})
        res = cp.handle({"event": "pre_command", "cwd": self.repo, "command": "rm -rf ~"})
        self.assertEqual(res.code, 2)
        rec = self.read_trace()[-1]
        self.assertEqual(rec["outcome"], "deny")
        self.assertEqual(rec["rule_id"], "rm-recursive-dangerous")
        self.assertEqual(rec["cmd_len"], len("rm -rf ~"))
        dump = json.dumps(rec)
        self.assertNotIn("rm -rf ~", dump)
        self.assertIn("cmd_hash", rec)

    def test_session_id_flows_from_event(self):
        self.config(trace={"enabled": True})
        cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls", "session_id": "sess-123"})
        rec = self.read_trace()[-1]
        self.assertEqual(rec["session_id"], "sess-123")

    def test_pre_commit_nested_scan_secrets_shares_trace_id_and_redacts_value(self):
        self.config(trace={"enabled": True})
        self.stage("src/config.py", f"KEY = '{FAKE_AWS}'\n")
        p = self.run_cli("pre-commit")
        self.assertEqual(p.returncode, 2)
        records = self.read_trace()
        spans = {r["span"] for r in records}
        self.assertEqual(spans, {"pre_commit", "scan_secrets"})
        trace_ids = {r["trace_id"] for r in records}
        self.assertEqual(len(trace_ids), 1)
        nested = next(r for r in records if r["span"] == "scan_secrets")
        self.assertEqual(nested["parent_id"], f"{nested['trace_id']}:pre_commit")
        self.assertNotIn(FAKE_AWS, json.dumps(records))

    def test_trace_write_swallows_errors_even_in_strict_mode(self):
        # RepoTestCase.setUp sets CODING_PLUGIN_STRICT=1; a tracing failure must still never escalate,
        # unlike a failure inside an actual guard (see test_config_and_failopen.test_strict_mode_raises).
        self.assertEqual(os.environ.get("CODING_PLUGIN_STRICT"), "1")
        with mock.patch.object(cp, "_ensure_state_dir", side_effect=OSError("boom")):
            cp._trace_write(self.repo, {"span": "x"})  # must not raise
            self.config(trace={"enabled": True})
            res = cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        self.assertEqual(res.code, 0)

    def test_rotation_at_max_bytes(self):
        self.config(trace={"enabled": True, "maxBytes": 10})
        cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})
        d = os.path.join(self.repo, ".coding-plugin", "log")
        self.assertTrue(os.path.isfile(os.path.join(d, "trace.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(d, "trace.jsonl.1")))

    def test_cli_trace_tail(self):
        self.config(trace={"enabled": True})
        for _ in range(3):
            self.run_cli("guard-command", "ls")
        p = self.run_cli("trace", "tail", "--lines", "2")
        self.assertEqual(p.returncode, 0)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            rec = json.loads(ln)
            self.assertEqual(rec["span"], "guard_command")

    def test_cli_trace_show_by_trace_id(self):
        self.config(trace={"enabled": True})
        self.run_cli("guard-command", "ls")
        trace_id = self.read_trace()[-1]["trace_id"]
        p = self.run_cli("trace", "show", "--trace-id", trace_id)
        self.assertEqual(p.returncode, 0)
        self.assertIn(trace_id, p.stdout)

    def test_unknown_trace_config_key_type_warns(self):
        self.write(".coding-plugin.json", json.dumps({"trace": "not-a-dict"}))
        cfg, warnings = cp.load_config(self.repo)
        self.assertFalse(cfg["trace"]["enabled"])
        self.assertTrue(any("trace" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
