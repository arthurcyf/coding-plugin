---
name: init
description: Set up coding-plugin in a project. Detect the stack, generate scripts/check, AGENTS.md, .coding-plugin.json, git hooks and a CI workflow, and wire up the agents the user works with. Asks before overwriting anything.
---
# Set up coding-plugin in this project

The installer ships with this skill: `{{skill_dir}}/assets/coding_plugin.py` (Python 3.9 or later, standard library only). It vendors the core into `.coding-plugin/bin/`, copies the templates, and never overwrites a file without explicit approval. On Windows, use `python` or `py -3` wherever this skill says `python3`.

## 1. Inspect
- Confirm you are at the repository root (`git rev-parse --show-toplevel`). If this isn't a git repository, ask whether to run `git init`.
- Detect the stack from marker files:
  - `pyproject.toml`, `requirements*.txt` or `setup.py`: python
  - `package.json`: node
  - `go.mod`: go
  - `Cargo.toml`: rust

  If several match, or none do, ask which one `scripts/check` should target (`none` is allowed).
- Note what already exists: AGENTS.md, CLAUDE.md, scripts/check, a Makefile `check` target, formatter and linter configs, and any git hooks manager (husky, pre-commit, lefthook, an existing `core.hooksPath`).
- Note any existing CI: `.github/workflows/*`, `.gitlab-ci.yml`, `bitbucket-pipelines.yml`, `azure-pipelines.yml`, `Jenkinsfile`, `.circleci/config.yml`. Note what each pipeline already runs (tests? lint?), and where code is hosted (`git remote get-url origin`).
- If another hooks manager is in use, explain that `core.hooksPath` will take over from it, and ask whether to go ahead or skip the git hooks.

## 2. Ask (in one message)
1. Which agents do you use? Any of: claude-code, codex, cursor, gemini, copilot.
2. Is the detected stack right?
3. Are there protected paths beyond the defaults, such as `migrations/**` or `infra/prod/**`?
4. CI. Ask according to what you found:
   - **A pipeline already exists:** offer three choices.
     - (a) *Add one step to your existing pipeline (recommended):* use `--ci none`, then edit the pipeline in step 5.
     - (b) *Add a separate coding-plugin pipeline:* use `--ci <system>`. This is only possible where that CI system allows a second file (GitHub Actions); it may run your tests twice.
     - (c) *Skip CI:* use `--ci none`.
   - **No pipeline:** confirm the system, suggesting the one that matches the remote: github, gitlab, bitbucket, azure, or none. For anything else (Jenkins, CircleCI…), use `--ci none` and show the step to add.

## 3. Dry run
```
python3 "{{skill_dir}}/assets/coding_plugin.py" install --target . --stack <stack> --agents <a,b> --ci <choice> --dry-run
```
Show the user the report.
- For each `conflict` line, show what would change and ask, file by file, whether to overwrite it, merge it by hand, or skip it.
- An `existing` line means an existing pipeline file. It is never replaced; handle it with the CI choice above.

## 4. Install
Run the same command without `--dry-run`, adding `--overwrite <path>` for each file the user approved. This also runs `git config core.hooksPath .githooks`.

## 5. Tailor the generated files
- **scripts/check:** adapt the template to the project's real tools (its test runner, linter and typechecker from package.json scripts, pyproject or the Makefile). It must check formatting, lint, typecheck if there is one, and run the tests, exiting non-zero on any failure. Run it once.
- **AGENTS.md:** fill every section from what you actually read in the repository: overview, architecture (key directories, one line each), commands, conventions (taken from configs and existing code) and definition of done. Keep it under about 150 lines and remove the placeholder comments. If an AGENTS.md already existed, merge into it rather than replacing it.
- **.coding-plugin.json:** add the protected paths from step 2.
- **Existing CI** (choice a only): the install report's note shows the step for that CI system. Place it after the pipeline's dependency-install step, in the job that runs on pull requests and on the main branch.
  - If the pipeline already runs the project's tests and lint, add only `python3 .coding-plugin/bin/coding_plugin.py scan-secrets --all`, so the tests don't run twice.
  - Make sure Python 3 is available in that job (most images and hosted runners have it; slim Go and Rust images may not).
  - Show the user the exact diff and edit the file only after they approve.
- **CLAUDE.md** (Claude Code only): it must contain `@AGENTS.md` and only Claude-specific notes.
- If the install report says an agent needs extra setup (for example Gemini's `context.fileName`), do it only with the user's approval.

## 6. Verify and report
- Run the check command and `python3 .coding-plugin/bin/coding_plugin.py scan-secrets --all`.
- Report the files created or changed, the check result, and what is now enforced: agent hooks, git hooks (pre-commit, pre-push) and CI (which pipeline, and whether it is new or an added step).
- Next steps for the user:
  - Claude Code: `/plugin marketplace add arthurcyf/coding-plugin`, then `/plugin install coding-plugin@coding-plugin`, if the plugin isn't installed yet.
  - Commit the new files on a branch and open a pull request so CI runs.
- Don't commit or push unless the user asks.
