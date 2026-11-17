import unittest

from helpers import RepoTestCase, cp, run_git

# Built at runtime so this repository never contains a literal secret-shaped string.
FAKE_AWS = "AKIA" + "Z7Q2" * 4
FAKE_GH = "ghp_" + "a1B2" * 9


class ScanSecrets(RepoTestCase):
    def stage(self, rel, content):
        self.write(rel, content)
        run_git(self.repo, "add", rel)

    def test_clean_stage(self):
        self.stage("src/app.py", "print('hello')\n")
        self.assertEqual(cp.scan_secrets(self.ctx()).code, 0)

    def test_staged_aws_key_blocks_without_printing_value(self):
        self.stage("src/config.py", f"KEY = '{FAKE_AWS}'\n")
        res = cp.scan_secrets(self.ctx())
        self.assertEqual(res.code, 2)
        self.assertIn("src/config.py:1: aws-access-key-id", res.message)
        self.assertNotIn(FAKE_AWS, res.message)

    def test_line_numbers(self):
        self.stage("a.txt", "one\ntwo\n")
        self.commit()
        self.stage("a.txt", f"one\ntwo\nthree {FAKE_GH}\n")
        res = cp.scan_secrets(self.ctx())
        self.assertIn("a.txt:3: github-token", res.message)

    def test_allow_marker(self):
        self.stage("tests/fixture.py", f"KEY = '{FAKE_AWS}'  # coding-plugin:allow-secret\n")
        self.assertEqual(cp.scan_secrets(self.ctx()).code, 0)

    def test_documented_example_value_allowed(self):
        self.stage("docs.md", "Example: AKIAIOSFODNN7EXAMPLE\n")
        self.assertEqual(cp.scan_secrets(self.ctx()).code, 0)

    def test_private_key_header(self):
        header = "-----BEGIN " + "RSA PRIVATE KEY-----"
        self.stage("deploy/key.txt", header + "\nabc\n")
        self.assertIn("private-key", cp.scan_secrets(self.ctx()).message)

    def test_secret_file_staged(self):
        self.stage(".env", "X=1\n")
        res = cp.scan_secrets(self.ctx())
        self.assertIn(".env: secrets file staged", res.message)

    def test_env_example_ok(self):
        self.stage(".env.example", "X=\n")
        self.assertEqual(cp.scan_secrets(self.ctx()).code, 0)

    def test_full_scan_of_tracked_files(self):
        self.write("src/old.py", f"T = '{FAKE_GH}'\n")
        self.commit()
        self.assertEqual(cp.scan_secrets(self.ctx(), staged=True).code, 0)
        res = cp.scan_secrets(self.ctx(), staged=False)
        self.assertIn("src/old.py:1: github-token", res.message)

    def test_disabled(self):
        self.config(disabledGuards=["secret-scan"])
        self.stage("x.py", f"K='{FAKE_AWS}'\n")
        self.assertEqual(cp.scan_secrets(self.ctx()).code, 0)


if __name__ == "__main__":
    unittest.main()
