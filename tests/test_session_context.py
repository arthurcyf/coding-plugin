import os
import unittest

from helpers import RepoTestCase, cp, run_git


class SessionContext(RepoTestCase):
    def test_warns_on_main(self):
        res = cp.session_context(self.ctx())
        self.assertEqual(res.code, 0)
        self.assertIn("Branch: main", res.context)
        self.assertIn("WARNING: you are on protected branch 'main'", res.context)

    def test_feature_branch_and_dirty_count(self):
        run_git(self.repo, "switch", "-q", "-c", "feature/y")
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        res = cp.session_context(self.ctx())
        self.assertIn("Branch: feature/y; 2 uncommitted change(s).", res.context)
        self.assertNotIn("WARNING", res.context)

    def test_reports_check_command_and_config_warnings(self):
        self.write("scripts/check", "#!/bin/sh\n")
        self.write(".coding-plugin.json", "{not json")
        res = cp.session_context(self.ctx())
        self.assertIn("Check command: `scripts/check`", res.context)
        self.assertIn("Config warning", res.context)

    def test_vendored_version_mismatch(self):
        self.write(".coding-plugin/bin/coding_plugin.py", '__version__ = "0.0.1"\n')
        res = cp.session_context(self.ctx())
        self.assertIn("vendors coding-plugin core 0.0.1", res.context)

    def test_not_a_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        res = cp.session_context(cp.Ctx(plain))
        self.assertIn("Not a git repository", res.context)

    def test_state_dir_is_gitignored(self):
        cp.session_context(self.ctx(), "s")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".coding-plugin", "state", "run-checks.json")))
        status = run_git(self.repo, "status", "--porcelain").stdout
        self.assertEqual(status.strip(), "")


if __name__ == "__main__":
    unittest.main()
