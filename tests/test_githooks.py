"""End-to-end: real git commits/pushes through the installed .githooks (no agent involved)."""
import os
import unittest

from helpers import RepoTestCase, cp, run_git

FAKE_AWS = "AKIA" + "Q9W8" * 4
CHECK = """import os, sys
sys.exit(1 if os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'FAIL')) else 0)
"""


class GitHooks(RepoTestCase):
    def setUp(self):
        super().setUp()
        res = cp.install(self.repo, "none", [], False)
        self.assertEqual(res.code, 0, res.message)
        self.write_check(CHECK)
        self.config(checkCommand="scripts/check.py")
        self.commit("install coding-plugin")
        self.remote = os.path.join(self.tmp, "remote.git")
        run_git(self.tmp, "init", "-q", "--bare", self.remote)
        run_git(self.repo, "remote", "add", "origin", self.remote)

    def git_commit(self, msg="change"):
        run_git(self.repo, "add", "-A")
        return run_git(self.repo, "commit", "-q", "-m", msg, check=False)

    # pre-commit ----------------------------------------------------------

    def test_normal_commit_passes(self):
        self.write("src/a.py", "x = 1\n")
        p = self.git_commit()
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_secret_commit_blocked(self):
        self.write("src/a.py", f"KEY = '{FAKE_AWS}'\n")
        p = self.git_commit()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("aws-access-key-id", p.stderr)
        self.assertNotIn(FAKE_AWS, p.stderr)

    def test_env_file_commit_blocked(self):
        self.write(".env", "X=1\n")
        p = self.git_commit()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(".env", p.stderr)

    def test_bypass_with_env(self):
        self.write("src/a.py", f"KEY = '{FAKE_AWS}'\n")
        os.environ["CODING_PLUGIN_DISABLE"] = "secret-scan"
        p = self.git_commit()
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_pre_commit_check_optional(self):
        self.config(checkCommand="scripts/check.py", preCommitCheck=True)
        self.git_commit("config")
        self.write("FAIL", "")
        p = self.git_commit()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("scripts/check.py", p.stderr)

    # pre-push ------------------------------------------------------------

    def push(self, *args):
        return run_git(self.repo, "push", "-q", *args, check=False)

    def test_push_and_force_push_to_main(self):
        p = self.push("-u", "origin", "main")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.write("b.txt", "b")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "--amend", "-m", "rewritten")
        p = self.push("--force", "origin", "main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("non-fast-forward", p.stderr)

    def test_force_push_feature_branch_allowed(self):
        run_git(self.repo, "switch", "-q", "-c", "feature/x")
        self.assertEqual(self.push("-u", "origin", "feature/x").returncode, 0)
        run_git(self.repo, "commit", "-q", "--amend", "-m", "rewritten", "--allow-empty")
        p = self.push("--force", "origin", "feature/x")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_delete_main_blocked(self):
        self.assertEqual(self.push("origin", "main").returncode, 0)
        p = self.push("origin", "--delete", "main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("deleting protected branch", p.stderr)

    def test_failing_checks_block_push(self):
        self.write("FAIL", "")
        p = self.push("origin", "main")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("push blocked", p.stderr)

    def test_pre_push_unit(self):
        zero = "0" * 40
        head = run_git(self.repo, "rev-parse", "HEAD").stdout.strip()
        res = cp.pre_push(self.ctx(), f"refs/heads/main {zero} refs/heads/main {head}\n")
        self.assertTrue(res.blocked)
        res = cp.pre_push(self.ctx(), f"refs/heads/main {head} refs/heads/main {zero}\n")
        self.assertFalse(res.blocked)

    def test_ci_entry_point(self):
        self.assertEqual(cp.ci(self.ctx()).code, 0)
        self.write("FAIL", "")
        self.assertEqual(cp.ci(self.ctx()).code, 2)


if __name__ == "__main__":
    unittest.main()
