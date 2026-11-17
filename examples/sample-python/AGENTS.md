# AGENTS.md

## Project overview
`calc` is a tiny arithmetic library used to try out coding-plugin end to end.

## Architecture
- `calc/core.py`: the functions.
- `tests/`: unittest tests, one file per module.

## Commands
- Check everything: `scripts/check`
- Tests only: `python3 -m unittest discover -s tests -t .`

## Conventions
- Standard library only. Type hints on public functions.
- Raise built-in exceptions with a clear message; never return sentinel values.

## Definition of done
- `scripts/check` passes.
- New behaviour has tests; bug fixes start with a failing test.
- No edits to protected paths (see `.coding-plugin.json`).

## Boundaries
- Work on a feature branch, never directly on main.
- Never read or commit `.env`.
