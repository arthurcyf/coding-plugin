import os
import shutil
import unittest

from helpers import RepoTestCase, cp

# Check script controlled by a file: exits with the number in .check-result (default 0).
CHECK = """import os, sys
p = os.path.join(os.path.dirname(__file__), '..', '.check-result')
code = int(open(p).read()) if os.path.exists(p) else 0
print('ran check; exit', code)
if code:
    print('FAILED test_something: assert 1 == 2')
sys.exit(code)
"""


class RunChecks(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.write_check(CHECK)
        self.write(".gitignore", ".check-result\n")
        self.commit("add check")

    def set_result(self, code: int) -> None:
        with open(os.path.join(self.repo, ".check-result"), "w") as fh:
            fh.write(str(code))

    def dirty(self, n: int = 1) -> None:
        self.write(f"src/file{n}.py", f"x = {n}\n")

    def stop(self, session="s1", continuing=False):
        return cp.run_checks(self.ctx(), session, continuing)

    def test_no_check_command_allows(self):
        os.remove(os.path.join(self.repo, "scripts", "check.py"))
        self.dirty()
        self.assertEqual(self.stop().code, 0)

    def test_clean_tree_skips(self):
        self.set_result(1)
        self.assertEqual(self.stop().code, 0)

    def test_pass(self):
        self.dirty()
        self.assertEqual(self.stop().code, 0)

    def test_failure_blocks_with_summary(self):
        self.set_result(1)
        self.dirty()
        res = self.stop()
        self.assertEqual(res.code, 2)
        self.assertIn("FAILED test_something", res.message)
        self.assertIn("attempt 1 of 3", res.message)

    def test_retry_cap_then_gives_up(self):
        self.set_result(1)
        self.dirty()
        self.assertEqual(self.stop(continuing=False).code, 2)
        self.assertEqual(self.stop(continuing=True).code, 2)
        self.assertEqual(self.stop(continuing=True).code, 2)
        res = self.stop(continuing=True)
        self.assertEqual(res.code, 0)
        self.assertIn("still failing after 3 attempts", res.notice)

    def test_new_turn_resets_attempts(self):
        self.config(stopRetryCap=1)
        self.set_result(1)
        self.dirty()
        self.assertEqual(self.stop(continuing=False).code, 2)
        self.assertEqual(self.stop(continuing=True).code, 0)  # cap reached
        self.assertEqual(self.stop(continuing=False).code, 2)  # new user turn: counter reset

    def test_unchanged_after_green_skips(self):
        self.dirty()
        self.assertEqual(self.stop().code, 0)
        self.set_result(1)  # would fail now, but nothing in the tree changed (.check-result is ignored)
        self.assertEqual(self.stop().code, 0)
        self.dirty(2)
        self.assertEqual(self.stop().code, 2)

    def test_session_baseline_skips_untouched_dirty_tree(self):
        self.dirty()
        self.set_result(1)
        cp.session_context(self.ctx(), "s1")  # records baseline
        self.assertEqual(self.stop("s1").code, 0)  # the agent changed nothing
        self.dirty(2)
        self.assertEqual(self.stop("s1").code, 2)

    def test_disabled_by_config(self):
        self.config(requireChecksOnStop=False)
        self.set_result(1)
        self.dirty()
        self.assertEqual(self.stop().code, 0)

    def test_timeout_does_not_block(self):
        self.write_check("import time\ntime.sleep(30)\n")
        self.config(checkTimeoutSec=1)
        self.dirty()
        res = self.stop()
        self.assertEqual(res.code, 0)
        self.assertIn("timed out", res.notice)

    def test_force_runs_even_when_clean(self):
        self.set_result(1)
        self.assertEqual(cp.run_checks(self.ctx(), "s", False, force=True).code, 2)

    def test_custom_check_command(self):
        self.config(checkCommand="scripts/check.py")
        self.set_result(1)
        self.dirty()
        self.assertEqual(self.stop().code, 2)

    @unittest.skipUnless(cp.find_sh(), "sh not available")
    def test_shell_script_check(self):
        os.remove(os.path.join(self.repo, "scripts", "check.py"))
        self.write("scripts/check", "#!/bin/sh\necho shell-check-ran\nexit 1\n")
        self.dirty()
        res = self.stop()
        self.assertEqual(res.code, 2)
        self.assertIn("shell-check-ran", res.message)

    def test_cli_stop_exit_code(self):
        self.set_result(1)
        self.dirty()
        p = self.run_cli("run-checks", "--session", "cli")
        self.assertEqual(p.returncode, 2)
        self.assertIn("FAILED", p.stderr)

    def test_summary_trims_and_strips_ansi(self):
        out = "\n".join(f"\x1b[31mline {i}\x1b[0m" for i in range(200))
        s = cp.summarize(out, max_lines=10)
        self.assertIn("[190 earlier lines omitted]", s)
        self.assertNotIn("\x1b", s)
        self.assertTrue(s.endswith("line 199"))

    def test_makefile_check_target_detected(self):
        os.remove(os.path.join(self.repo, "scripts", "check.py"))
        self.write("Makefile", "check:\n\techo ok\n")
        self.assertEqual(cp.resolve_check_command(self.ctx()), "make check")


if __name__ == "__main__":
    unittest.main()
