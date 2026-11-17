import sys
import unittest

from helpers import RepoTestCase, cp

# A fake formatter: uppercases the file it is given.
UPPER = "import sys\np = sys.argv[1]\ns = open(p).read()\nopen(p, 'w').write(s.upper())\n"


class FormatFile(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.fmt = self.write("tools/upper.py", UPPER)

    def test_runs_configured_formatter(self):
        self.config(formatters={".txt": [sys.executable, self.fmt, "{file}"]})
        self.write("notes.txt", "hello\n")
        res = cp.format_file(self.ctx(), "notes.txt")
        self.assertEqual(res.code, 0)
        self.assertEqual(self.read("notes.txt"), "HELLO\n")

    def test_fallback_chain_skips_missing_formatter(self):
        self.config(formatters={".txt": [["definitely-not-installed-fmt", "{file}"], [sys.executable, self.fmt, "{file}"]]})
        self.write("notes.txt", "hello\n")
        cp.format_file(self.ctx(), "notes.txt")
        self.assertEqual(self.read("notes.txt"), "HELLO\n")

    def test_missing_formatter_never_fails(self):
        self.config(formatters={".txt": ["definitely-not-installed-fmt", "{file}"]})
        self.write("notes.txt", "hello\n")
        res = cp.format_file(self.ctx(), "notes.txt")
        self.assertEqual(res.code, 0)
        self.assertEqual(self.read("notes.txt"), "hello\n")

    def test_failing_formatter_never_fails(self):
        bad = self.write("tools/bad.py", "import sys\nsys.exit(3)\n")
        self.config(formatters={".txt": [sys.executable, bad, "{file}"]})
        self.write("notes.txt", "hello\n")
        self.assertEqual(cp.format_file(self.ctx(), "notes.txt").code, 0)

    def test_unknown_extension_and_missing_file(self):
        self.assertEqual(cp.format_file(self.ctx(), "nothing.zzz").code, 0)
        self.write("data.zzz", "x")
        self.assertEqual(cp.format_file(self.ctx(), "data.zzz").code, 0)

    def test_respects_format_on_edit_false(self):
        self.config(formatOnEdit=False, formatters={".txt": [sys.executable, self.fmt, "{file}"]})
        self.write("notes.txt", "hello\n")
        cp.format_file(self.ctx(), "notes.txt")
        self.assertEqual(self.read("notes.txt"), "hello\n")

    def test_skips_files_outside_repo_and_vendor_dirs(self):
        self.config(formatters={".txt": [sys.executable, self.fmt, "{file}"]})
        self.write("node_modules/pkg/a.txt", "hello\n")
        cp.format_file(self.ctx(), "node_modules/pkg/a.txt")
        self.assertEqual(self.read("node_modules/pkg/a.txt"), "hello\n")

    def test_prettier_only_from_local_node_modules(self):
        chain = cp._default_formatters(self.ctx(), ".ts")
        self.assertEqual(chain[0][0], "{root}/node_modules/.bin/prettier")
        self.assertIsNone(cp.resolve_exe("{root}/node_modules/.bin/prettier".replace("{root}", self.repo), self.repo))

    def test_python_prefers_black_when_configured(self):
        self.write("pyproject.toml", "[tool.black]\nline-length = 100\n")
        self.assertEqual(cp._default_formatters(self.ctx(), ".py")[0][0], "black")


if __name__ == "__main__":
    unittest.main()
