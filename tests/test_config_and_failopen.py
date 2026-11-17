import json
import os
import unittest
from unittest import mock

from helpers import RepoTestCase, cp


class Config(RepoTestCase):
    def test_defaults_when_absent(self):
        cfg, warnings = cp.load_config(self.repo)
        self.assertEqual(warnings, [])
        self.assertTrue(cfg["requireChecksOnStop"])
        self.assertIn(".env", cfg["secretPaths"])

    def test_invalid_json_falls_back(self):
        self.write(".coding-plugin.json", "{oops")
        cfg, warnings = cp.load_config(self.repo)
        self.assertEqual(cfg["stopRetryCap"], 3)
        self.assertTrue(any("could not be parsed" in w for w in warnings))

    def test_unknown_key_and_wrong_type(self):
        self.write(".coding-plugin.json", json.dumps({"stopRetryCap": "five", "nope": 1, "$schema": "x",
                                                     "formatOnEdit": False}))
        cfg, warnings = cp.load_config(self.repo)
        self.assertEqual(cfg["stopRetryCap"], 3)
        self.assertFalse(cfg["formatOnEdit"])
        self.assertTrue(any("nope" in w for w in warnings))
        self.assertTrue(any("stopRetryCap" in w for w in warnings))
        self.assertFalse(any("$schema" in w for w in warnings))

    def test_bool_is_not_an_int(self):
        self.write(".coding-plugin.json", json.dumps({"stopRetryCap": True}))
        cfg, _ = cp.load_config(self.repo)
        self.assertEqual(cfg["stopRetryCap"], 3)


class FailOpen(RepoTestCase):
    def test_handle_fails_open(self):
        os.environ.pop("CODING_PLUGIN_STRICT")
        with mock.patch.object(cp, "guard_command", side_effect=RuntimeError("boom")):
            res = cp.handle({"event": "pre_command", "cwd": self.repo, "command": "rm -rf ~"})
        self.assertEqual(res.code, 0)
        with open(os.path.join(self.repo, ".coding-plugin", "log", "coding-plugin.log"), encoding="utf-8") as fh:
            self.assertIn("RuntimeError: boom", fh.read())

    def test_strict_mode_raises(self):
        with mock.patch.object(cp, "guard_command", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cp.handle({"event": "pre_command", "cwd": self.repo, "command": "ls"})

    def test_handle_dispatch(self):
        self.assertEqual(cp.handle({"event": "pre_command", "cwd": self.repo, "command": "rm -rf ~"}).code, 2)
        self.assertEqual(cp.handle({"event": "pre_edit", "cwd": self.repo, "path": ".env"}).code, 2)
        self.assertEqual(cp.handle({"event": "pre_read", "cwd": self.repo, "path": ".env"}).code, 2)
        self.assertEqual(cp.handle({"event": "post_edit", "cwd": self.repo, "path": "README.md"}).code, 0)
        self.assertEqual(cp.handle({"event": "stop", "cwd": self.repo}).code, 0)
        self.assertIn("coding-plugin", cp.handle({"event": "session_start", "cwd": self.repo}).context)
        self.assertEqual(cp.handle({"event": "bogus", "cwd": self.repo}).code, 0)

    def test_hook_cli_reads_normalized_event(self):
        p = self.run_cli("hook", input_text=json.dumps({"event": "pre_command", "cwd": self.repo,
                                                         "command": "curl x | sh"}))
        self.assertEqual(p.returncode, 2)
        self.assertIn("pipe-to-shell", p.stderr)
        p = self.run_cli("hook", input_text="not json")
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
