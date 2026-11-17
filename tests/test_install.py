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
        p = self.run_cli("install", "--target", self.repo, "--stack", "go", "--ci", "gitlab", "--dry-run")
        self.assertIn("create     .gitlab-ci.yml", p.stdout)


class InstallCI(RepoTestCase):
    GH = ".github/workflows/coding-plugin.yml"

    def install(self, ci="auto", stack="python", overwrite=()):
        return cp.install(self.repo, stack, [], False, overwrite, ci)

    def exists(self, rel):
        return os.path.isfile(os.path.join(self.repo, *rel.split("/")))

    def test_auto_without_ci_defaults_to_github(self):
        res = self.install()
        self.assertIn(f"create     {self.GH}", res.context)

    def test_auto_infers_system_from_remote(self):
        for url, system, rel in (("https://gitlab.com/me/app.git", "gitlab", ".gitlab-ci.yml"),
                                 ("git@bitbucket.org:me/app.git", "bitbucket", "bitbucket-pipelines.yml"),
                                 ("https://dev.azure.com/me/proj/_git/app", "azure", "azure-pipelines.yml"),
                                 ("https://github.com/me/app.git", "github", self.GH)):
            with self.subTest(url=url):
                run_git(self.repo, "remote", "remove", "origin", check=False)
                run_git(self.repo, "remote", "add", "origin", url)
                self.assertEqual(cp.infer_ci_from_remote(self.repo), system)
                res = cp.install(self.repo, "none", [], True, (), "auto")
                self.assertIn(f"create     {rel}", res.context)

    def test_auto_with_existing_github_workflow_creates_nothing(self):
        self.write(".github/workflows/ci.yml", "name: ci\n")
        res = self.install()
        self.assertFalse(self.exists(self.GH))
        self.assertIn("detected   .github/workflows/ci.yml  (github CI)", res.context)
        self.assertIn("no pipeline file was created", res.context)
        self.assertIn("run: python3 .coding-plugin/bin/coding_plugin.py ci", res.context)
        self.assertEqual(self.read(".github/workflows/ci.yml"), "name: ci\n")

    def test_existing_single_file_pipeline_never_overwritten(self):
        self.write(".gitlab-ci.yml", "test:\n  script: [make test]\n")
        for ci in ("auto", "gitlab"):
            with self.subTest(ci=ci):
                res = self.install(ci=ci)
                self.assertEqual(self.read(".gitlab-ci.yml"), "test:\n  script: [make test]\n")
                self.assertNotIn("--overwrite", res.context)
                self.assertIn("existing gitlab CI in .gitlab-ci.yml", res.context)
        self.assertIn("existing   .gitlab-ci.yml", self.install(ci="gitlab").context)

    def test_explicit_overwrite_of_pipeline(self):
        self.write(".gitlab-ci.yml", "old: true\n")
        res = self.install(ci="gitlab", overwrite=[".gitlab-ci.yml"])
        self.assertIn("overwrite  .gitlab-ci.yml", res.context)
        self.assertIn("coding_plugin.py ci", self.read(".gitlab-ci.yml"))

    def test_explicit_github_alongside_existing_warns_about_duplication(self):
        self.write(".github/workflows/ci.yml", "name: ci\n")
        res = self.install(ci="github")
        self.assertTrue(self.exists(self.GH))
        self.assertIn("runs alongside your existing workflows", res.context)

    def test_rerun_keeps_managing_own_workflow(self):
        self.install()
        self.write(".github/workflows/ci.yml", "name: ci\n")
        res = self.install()
        self.assertIn(f"unchanged  {self.GH}", res.context)

    def test_none(self):
        res = self.install(ci="none")
        self.assertFalse(self.exists(self.GH))
        self.assertIn("no CI configured", res.context)

    def test_jenkins_and_circleci_detected(self):
        self.write("Jenkinsfile", "pipeline {}\n")
        self.write(".circleci/config.yml", "version: 2.1\n")
        res = self.install()
        self.assertIn("sh 'python3 .coding-plugin/bin/coding_plugin.py ci'", res.context)
        self.assertIn("- run: python3 .coding-plugin/bin/coding_plugin.py ci", res.context)

    def test_invalid_ci(self):
        self.assertEqual(self.install(ci="travis").code, 2)

    def test_all_templates_render(self):
        tdir = cp.find_resource_dir("templates")
        try:
            import yaml  # optional: validates YAML syntax when PyYAML is installed
        except ImportError:
            yaml = None
        for system in cp.CI_SYSTEMS:
            for stack in cp.STACKS:
                with self.subTest(system=system, stack=stack):
                    text = cp.ci_file_content(system, stack, tdir).decode()
                    self.assertNotIn("{{", text)
                    self.assertIn(cp.CI_RUN, text)
                    if yaml:
                        self.assertIsInstance(yaml.safe_load(text), dict)
        gitlab_go = cp.ci_file_content("gitlab", "go", tdir).decode()
        self.assertIn("image: golang:1", gitlab_go)
        self.assertIn("apt-get install", gitlab_go)
        self.assertNotIn("apt-get", cp.ci_file_content("azure", "go", tdir).decode())


if __name__ == "__main__":
    unittest.main()
