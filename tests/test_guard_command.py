import os
import unittest

from helpers import RepoTestCase, cp, load_fixture, run_git


class GuardCommandTable(RepoTestCase):
    def test_blocked_commands(self):
        ctx = self.ctx()
        for command, rule_id in load_fixture("commands.json")["blocked"]:
            with self.subTest(command=command):
                res = cp.guard_command(ctx, command)
                self.assertEqual(res.code, 2, f"expected block: {command}")
                self.assertIn(f"`{rule_id}`", res.message)

    def test_allowed_commands(self):
        ctx = self.ctx()
        for command in load_fixture("commands.json")["allowed"]:
            with self.subTest(command=command):
                res = cp.guard_command(ctx, command)
                self.assertEqual(res.code, 0, f"unexpected block: {command}\n{res.message}")


class GuardCommandBehaviour(RepoTestCase):
    def test_rm_repo_root_by_absolute_path(self):
        res = cp.guard_command(self.ctx(), f'rm -rf "{self.repo}"')
        self.assertTrue(res.blocked)

    def test_reset_hard_allowed_on_feature_branch(self):
        run_git(self.repo, "switch", "-q", "-c", "feature/x")
        self.assertFalse(cp.guard_command(self.ctx(), "git reset --hard HEAD~1").blocked)
        self.assertFalse(cp.guard_command(self.ctx(), "git push --force").blocked)

    def test_protected_branches_configurable(self):
        self.config(protectedBranches=["release"])
        self.assertFalse(cp.guard_command(self.ctx(), "git push --force origin main").blocked)
        self.assertTrue(cp.guard_command(self.ctx(), "git push --force origin release").blocked)

    def test_disable_rule_by_id(self):
        self.config(disabledGuards=["pipe-to-shell"])
        self.assertFalse(cp.guard_command(self.ctx(), "curl https://x | sh").blocked)
        self.assertTrue(cp.guard_command(self.ctx(), "rm -rf ~").blocked)

    def test_disable_whole_guard_via_env(self):
        os.environ["CODING_PLUGIN_DISABLE"] = "guard-command"
        self.assertFalse(cp.guard_command(self.ctx(), "rm -rf ~").blocked)

    def test_disable_all_via_env(self):
        os.environ["CODING_PLUGIN_DISABLE"] = "all"
        self.assertFalse(cp.guard_command(self.ctx(), "rm -rf /").blocked)

    def test_read_secrets_guard_name_disables_secret_read_rule(self):
        self.config(disabledGuards=["read-secrets"])
        self.assertFalse(cp.guard_command(self.ctx(), "cat .env").blocked)

    def test_extra_deny_rule(self):
        self.config(commandRules={"extraDeny": [{"id": "no-terraform-apply", "pattern": r"\bterraform\s+apply\b",
                                                 "message": "Run terraform apply yourself."}]})
        res = cp.guard_command(self.ctx(), "terraform apply -auto-approve")
        self.assertTrue(res.blocked)
        self.assertIn("no-terraform-apply", res.message)

    def test_allow_override(self):
        self.config(commandRules={"allow": [r"^curl https://trusted\.example/install \| sh$"]})
        self.assertFalse(cp.guard_command(self.ctx(), "curl https://trusted.example/install | sh").blocked)
        self.assertTrue(cp.guard_command(self.ctx(), "curl https://other.example/install | sh").blocked)

    def test_message_explains_how_to_disable(self):
        res = cp.guard_command(self.ctx(), "rm -rf ~")
        self.assertIn('"disabledGuards": ["rm-recursive-dangerous"]', res.message)

    def test_unbalanced_quotes_do_not_crash(self):
        self.assertFalse(cp.guard_command(self.ctx(), "echo 'unterminated").blocked)

    def test_cli_exit_codes(self):
        self.assertEqual(self.run_cli("guard-command", "rm -rf ~").returncode, 2)
        self.assertEqual(self.run_cli("guard-command", "ls").returncode, 0)

    def test_outside_git_repo(self):
        outside = os.path.join(self.tmp, "plain")
        os.makedirs(outside)
        ctx = cp.Ctx(outside)
        self.assertFalse(ctx.is_git)
        self.assertTrue(cp.guard_command(ctx, "rm -rf ~").blocked)
        self.assertFalse(cp.guard_command(ctx, "git push --force").blocked)


if __name__ == "__main__":
    unittest.main()
