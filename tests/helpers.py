"""Shared test helpers: import the core module and build throwaway git repos."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(ROOT, "core", "scripts")
CORE = os.path.join(CORE_DIR, "coding_plugin.py")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

import coding_plugin as cp  # noqa: E402


def run_git(cwd: str, *args: str, check: bool = True, input_text: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        input=input_text,
        check=check,
    )


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def load_fixture(*parts: str):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as fh:
        return json.load(fh)


class RepoTestCase(unittest.TestCase):
    """Each test gets a fresh git repo on branch main with one commit."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, onerror=_rm_readonly)
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        run_git(self.repo, "init", "-q", "-b", "main")
        run_git(self.repo, "config", "user.email", "test@example.com")
        run_git(self.repo, "config", "user.name", "Test")
        run_git(self.repo, "config", "commit.gpgsign", "false")
        run_git(self.repo, "config", "core.autocrlf", "false")
        self.write("README.md", "# test\n")
        self.commit("initial")
        self._env = {k: os.environ.get(k) for k in ("CODING_PLUGIN_STRICT", "CODING_PLUGIN_DISABLE")}
        os.environ["CODING_PLUGIN_STRICT"] = "1"
        os.environ.pop("CODING_PLUGIN_DISABLE", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, rel: str, content: str) -> str:
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        return path

    def read(self, rel: str) -> str:
        with open(os.path.join(self.repo, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def commit(self, msg: str = "change") -> None:
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", msg, "--allow-empty")

    def config(self, **values) -> None:
        self.write(".coding-plugin.json", json.dumps(values))

    def ctx(self, cwd: str = None) -> "cp.Ctx":
        return cp.Ctx(cwd or self.repo)

    def run_cli(self, *args: str, input_text: str = None, cwd: str = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, CORE, *args],
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            input=input_text,
            encoding="utf-8",
        )

    def write_check(self, body: str) -> None:
        """Install scripts/check.py (run with the current interpreter, so no sh needed)."""
        self.write("scripts/check.py", body)
