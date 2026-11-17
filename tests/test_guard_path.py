import os
import unittest

from helpers import RepoTestCase, cp


class PathMatching(unittest.TestCase):
    def test_glob_semantics(self):
        cases = [
            (".env", [".env"], True),
            ("config/.env", [".env"], True),
            (".env.local", [".env.*"], True),
            (".env.example", [".env.*", "!.env.example"], False),
            ("secrets/a/b.json", ["secrets/**"], True),
            ("app/secrets/a.json", ["secrets/**"], False),
            ("app/secrets/a.json", ["**/secrets/**"], True),
            ("certs/server.pem", ["**/*.pem"], True),
            ("server.pem", ["**/*.pem"], True),
            ("migrations/0001.sql", ["migrations/**"], True),
            ("src/migrations.py", ["migrations/**"], False),
            ("SRC/.ENV", [".env"], True),
            ("docs/readme.md", ["*.pem"], False),
            ("build/out.txt", ["build/"], True),
            ("a/b/c.txt", ["a/*/c.txt"], True),
            ("a/x/y/c.txt", ["a/*/c.txt"], False),
        ]
        for target, patterns, expected in cases:
            with self.subTest(target=target, patterns=patterns):
                self.assertEqual(cp.path_matches(target, patterns), expected)


class GuardPath(RepoTestCase):
    def test_default_protected_paths(self):
        ctx = self.ctx()
        for path in (".env", ".env.production", "secrets/db.json", "certs/key.pem", ".git/config"):
            with self.subTest(path=path):
                self.assertTrue(cp.guard_path(ctx, path).blocked)
        for path in ("src/app.py", ".env.example", "README.md"):
            with self.subTest(path=path):
                self.assertFalse(cp.guard_path(ctx, path).blocked)

    def test_absolute_and_windows_style_paths(self):
        ctx = self.ctx()
        abs_env = os.path.join(self.repo, "config", ".env")
        self.assertTrue(cp.guard_path(ctx, abs_env).blocked)
        self.assertTrue(cp.guard_path(ctx, abs_env.replace("/", "\\") if os.name == "nt" else abs_env).blocked)
        self.assertFalse(cp.guard_path(ctx, os.path.join(self.repo, "src", "x.py")).blocked)

    def test_relative_to_subdirectory_cwd(self):
        sub = os.path.join(self.repo, "pkg")
        os.makedirs(sub)
        self.config(protectedPaths=["migrations/**"])
        ctx = cp.Ctx(sub)
        self.assertFalse(cp.guard_path(ctx, "migrations/1.sql").blocked)  # pkg/migrations is not root migrations
        self.assertTrue(cp.guard_path(ctx, "../migrations/1.sql").blocked)

    def test_custom_protected_paths_replace_defaults(self):
        self.config(protectedPaths=["migrations/**"])
        ctx = self.ctx()
        self.assertTrue(cp.guard_path(ctx, "migrations/0001_init.sql").blocked)
        self.assertFalse(cp.guard_path(ctx, ".env").blocked)

    def test_read_mode_uses_secret_paths(self):
        ctx = self.ctx()
        self.assertTrue(cp.guard_path(ctx, ".env", mode="read").blocked)
        self.assertTrue(cp.guard_path(ctx, os.path.expanduser("~/.ssh/id_ed25519"), mode="read").blocked)
        self.assertFalse(cp.guard_path(ctx, os.path.expanduser("~/.ssh/id_ed25519.pub"), mode="read").blocked)
        self.assertFalse(cp.guard_path(ctx, "src/app.py", mode="read").blocked)

    def test_disable(self):
        self.config(disabledGuards=["guard-path", "read-secrets"])
        ctx = self.ctx()
        self.assertFalse(cp.guard_path(ctx, ".env").blocked)
        self.assertFalse(cp.guard_path(ctx, ".env", mode="read").blocked)

    def test_cli(self):
        self.assertEqual(self.run_cli("guard-path", ".env").returncode, 2)
        self.assertEqual(self.run_cli("guard-path", "--mode", "read", ".env").returncode, 2)
        self.assertEqual(self.run_cli("guard-path", "src/x.py").returncode, 0)


if __name__ == "__main__":
    unittest.main()
