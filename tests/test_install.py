import os
import unittest

from helpers import CORE, RepoTestCase, cp, run_git


class Install(RepoTestCase):
    def install(self, stack="python", agents=("claude-code",), dry_run=False, overwrite=()):
        return cp.install(self.repo, stack, list(agents), dry_run, overwrite)

    def exists(self, rel):
        return os.path.isfile(os.path.join(self.repo, *rel.split("/")))

    def test_dry_run_writes_nothing(self):
        res = self.install(dry_run=True)
        self.assertIn("dry run", res.context)
        self.assertIn("create     AGENTS.md", res.context)
        self.assertFalse(self.exists("AGENTS.md"))
        self.assertFalse(self.exists(".coding-plugin/bin/coding_plugin.py"))

    def test_install_creates_files_and_sets_hooks_path(self):
        res = self.install()
        self.assertEqual(res.code, 0)
        for rel in (".coding-plugin/bin/coding_plugin.py", ".coding-plugin/bin/rules/commands.json",
                    ".coding-plugin/bin/rules/secrets.json", ".coding-plugin.json", ".githooks/pre-commit",
                    ".githooks/pre-push", ".github/workflows/coding-plugin.yml", "AGENTS.md", "CLAUDE.md",
                    "scripts/check", ".gitattributes"):
            with self.subTest(rel=rel):
                self.assertTrue(self.exists(rel))
        self.assertEqual(run_git(self.repo, "config", "core.hooksPath").stdout.strip(), ".githooks")
        with open(CORE, "rb") as a, open(os.path.join(self.repo, ".coding-plugin", "bin", "coding_plugin.py"), "rb") as b:
            self.assertEqual(a.read().replace(b"\r\n", b"\n"), b.read())
        self.assertIn("@AGENTS.md", self.read("CLAUDE.md"))
        if os.name != "nt":
            self.assertTrue(os.access(os.path.join(self.repo, ".githooks", "pre-commit"), os.X_OK))
        else:
            mode = run_git(self.repo, "ls-files", "-s", ".githooks/pre-commit").stdout.split()[0]
            self.assertEqual(mode, "100755")

    def test_rerun_is_idempotent(self):
        self.install()
        res = self.install()
        self.assertNotIn("create", res.context)
        self.assertNotIn("conflict", res.context)

    def test_never_overwrites_user_files_without_permission(self):
        self.write("AGENTS.md", "# mine\n")
        res = self.install()
        self.assertIn("conflict   AGENTS.md", res.context)
        self.assertEqual(self.read("AGENTS.md"), "# mine\n")
        res = self.install(overwrite=["AGENTS.md"])
        self.assertIn("overwrite  AGENTS.md", res.context)
        self.assertIn("Definition of done", self.read("AGENTS.md"))

    def test_existing_claude_md_with_import_is_left_alone(self):
        self.write("CLAUDE.md", "@AGENTS.md\n\nmy notes\n")
        res = self.install()
        self.assertNotIn("CLAUDE.md", res.context)
        self.assertEqual(self.read("CLAUDE.md"), "@AGENTS.md\n\nmy notes\n")

    def test_managed_files_are_updated(self):
        self.install()
        self.write(".coding-plugin/bin/coding_plugin.py", "old\n")
        res = self.install()
        self.assertIn("update     .coding-plugin/bin/coding_plugin.py", res.context)

    def test_gitattributes_lines_appended(self):
        self.write(".gitattributes", "*.png binary")
        self.install()
        text = self.read(".gitattributes")
        self.assertTrue(text.startswith("*.png binary\n"))
        self.assertIn(".githooks/* text eol=lf", text)

    def test_stack_specific_ci_and_check(self):
        self.install(stack="node", agents=())
        ci = self.read(".github/workflows/coding-plugin.yml")
        self.assertIn("actions/setup-node", ci)
        self.assertNotIn("{{stack-setup}}", ci)
        self.assertIn("run_script test", self.read("scripts/check"))
        self.assertFalse(self.exists("CLAUDE.md"))

    def test_stack_none_has_no_check_script(self):
        self.install(stack="none", agents=())
        self.assertFalse(self.exists("scripts/check"))

    def test_unknown_stack_rejected(self):
        self.assertEqual(cp.install(self.repo, "cobol", [], True).code, 2)

    def test_agent_notes(self):
        res = self.install(agents=("gemini", "codex", "nope"))
        self.assertIn("context", res.context)
        self.assertIn("codex: reads AGENTS.md natively", res.context)
        self.assertIn("unknown agent 'nope'", res.context)

    def test_cli(self):
        p = self.run_cli("install", "--target", self.repo, "--stack", "go", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("create     scripts/check", p.stdout)


if __name__ == "__main__":
    unittest.main()
