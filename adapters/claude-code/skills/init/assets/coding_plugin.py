#!/usr/bin/env python3
"""coding-plugin core: agent-neutral guards, formatting and checks.

Single file, Python 3.9+ standard library only. Works on macOS, Linux and
Windows.

Two ways in:
  * Agent adapters translate their hook payload into a *normalized event* and
    call ``handle(event)`` (or pipe it to ``coding_plugin.py hook``).
  * Git hooks, CI and humans call the subcommands directly
    (``coding_plugin.py --help``).

Normalized event (JSON)::

    {"event": "pre_command" | "pre_edit" | "pre_read" | "post_edit"
              | "stop" | "session_start",
     "cwd": "/abs/path", "command": "...", "path": "...",
     "session_id": "...", "continuing": false}

Exit codes: 0 = allow / pass, 2 = deny / block (reason on stderr).
The toolkit fails open: an internal error is logged to
``.coding-plugin/log/coding-plugin.log`` and the action is allowed (exit 0),
unless ``CODING_PLUGIN_STRICT=1`` is set, in which case it exits 70.

Tracing (off by default): set ``"trace": {"enabled": true}`` in
``.coding-plugin.json``, or ``CODING_PLUGIN_TRACE=1`` for one invocation, to
record what each guard decided to ``.coding-plugin/log/trace.jsonl``. See
``coding_plugin.py trace --help``.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

__version__ = "0.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"
STATE_DIR = ".coding-plugin"
CONFIG_FILE = ".coding-plugin.json"
EXIT_BLOCK = 2
EXIT_INTERNAL = 70
# Ignores itself too, so a repo that never ran `install` shows no untracked noise.
STATE_GITIGNORE = "# created by coding-plugin\n.gitignore\nlog/\nstate/\n"

GUARDS = (
    "guard-command",
    "guard-path",
    "read-secrets",
    "format-file",
    "run-checks",
    "secret-scan",
    "force-push",
)

DEFAULT_CONFIG: Dict[str, Any] = {
    "checkCommand": None,
    "checkTimeoutSec": 600,
    "requireChecksOnStop": True,
    "stopRetryCap": 3,
    "preCommitCheck": False,
    "formatOnEdit": True,
    "formatters": {},
    "protectedPaths": [
        ".env",
        ".env.*",
        "!.env.example",
        "!.env.sample",
        "!.env.template",
        "secrets/**",
        "**/*.pem",
        "**/*.key",
        ".git/**",
    ],
    "secretPaths": None,  # None -> rules/secrets.json "secretFiles"
    "protectedBranches": ["main", "master"],
    "commandRules": {"extraDeny": [], "allow": []},
    "disabledGuards": [],
    "trace": {"enabled": False, "maxBytes": 1_000_000},
}

_CONFIG_TYPES: Dict[str, Tuple[type, ...]] = {
    "checkCommand": (str, type(None)),
    "checkTimeoutSec": (int,),
    "requireChecksOnStop": (bool,),
    "stopRetryCap": (int,),
    "preCommitCheck": (bool,),
    "formatOnEdit": (bool,),
    "formatters": (dict,),
    "protectedPaths": (list,),
    "secretPaths": (list, type(None)),
    "protectedBranches": (list,),
    "commandRules": (dict,),
    "disabledGuards": (list,),
    "trace": (dict,),
}


# --------------------------------------------------------------------------
# Results, logging, state
# --------------------------------------------------------------------------


@dataclass
class Result:
    """Outcome of a core operation.

    code    0 = allow/pass, 2 = deny/block.
    message Blocking reason, written for the agent (stderr).
    context Extra context for the agent, e.g. at session start (stdout).
    notice  Non-blocking note for the user (stderr / systemMessage).
    """

    code: int = 0
    message: str = ""
    context: str = ""
    notice: str = ""

    @property
    def blocked(self) -> bool:
        return self.code == EXIT_BLOCK


def strict() -> bool:
    return os.environ.get("CODING_PLUGIN_STRICT") == "1"


def _ensure_state_dir(root: str, sub: str) -> str:
    base = os.path.join(root, STATE_DIR)
    os.makedirs(os.path.join(base, sub), exist_ok=True)
    gi = os.path.join(base, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(STATE_GITIGNORE)
    return os.path.join(base, sub)


def log(root: Optional[str], msg: str, level: str = "INFO") -> None:
    """Append to .coding-plugin/log/coding-plugin.log. Never raises."""
    if not root:
        return
    try:
        d = _ensure_state_dir(root, "log")
        path = os.path.join(d, "coding-plugin.log")
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {level} {msg}\n")
    except Exception:
        pass


def _trace_write(root: Optional[str], record: Dict[str, Any], max_bytes: int = 1_000_000) -> None:
    """Append one JSON record to .coding-plugin/log/trace.jsonl. Never raises.

    Trace fields must be identifiers, counts, booleans, hashes or repo-relative
    paths only -- never free-text command/file content or regex-matched
    substrings (the same "values not shown" discipline scan_secrets uses).
    """
    if not root:
        return
    try:
        d = _ensure_state_dir(root, "log")
        path = os.path.join(d, "trace.jsonl")
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


_RULE_ID_RE = re.compile(r"rule `([^`]+)`")


def _trace_rule_id(message: str) -> Optional[str]:
    """Best-effort rule id for a trace record, parsed from an existing block message. Never the matched value."""
    m = _RULE_ID_RE.search(message or "")
    return m.group(1) if m else None


def _trace_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _trace_path(path: str) -> str:
    return path.replace("\\", "/").strip()[:200]


def load_state(root: str, name: str) -> Dict[str, Any]:
    path = os.path.join(root, STATE_DIR, "state", name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: str, name: str, data: Dict[str, Any]) -> None:
    d = _ensure_state_dir(root, "state")
    path = os.path.join(d, name + ".json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Data files, config
# --------------------------------------------------------------------------


def find_resource_dir(name: str) -> Optional[str]:
    """Locate rules/ or templates/ next to this file or one level up."""
    for base in (HERE, os.path.dirname(HERE)):
        cand = os.path.join(base, name)
        if os.path.isdir(cand):
            return cand
    return None


@functools.lru_cache(maxsize=None)
def load_rules(filename: str) -> Dict[str, Any]:
    d = find_resource_dir("rules")
    if not d:
        return {}
    try:
        with open(os.path.join(d, filename), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(root: str) -> Tuple[Dict[str, Any], List[str]]:
    cfg: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    warnings: List[str] = []
    path = os.path.join(root, CONFIG_FILE)
    data: Any = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            warnings.append(f"{CONFIG_FILE} could not be parsed ({exc}); using defaults.")
            data = {}
        if not isinstance(data, dict):
            warnings.append(f"{CONFIG_FILE} must contain a JSON object; using defaults.")
            data = {}
    for key, value in data.items():
        if key.startswith(("$", "_")):
            continue  # $schema, _comment
        if key not in DEFAULT_CONFIG:
            warnings.append(f"unknown key {key!r} in {CONFIG_FILE} (ignored).")
            continue
        types = _CONFIG_TYPES[key]
        bad_bool = isinstance(value, bool) and bool not in types
        if bad_bool or not isinstance(value, types):
            warnings.append(f"{key!r} in {CONFIG_FILE} has the wrong type (ignored).")
            continue
        cfg[key] = value
    rules = cfg.get("commandRules") or {}
    cfg["commandRules"] = {
        "extraDeny": list(rules.get("extraDeny") or []),
        "allow": list(rules.get("allow") or []),
    }
    trace_cfg = cfg.get("trace") or {}
    cfg["trace"] = {
        "enabled": bool(trace_cfg.get("enabled", False)),
        "maxBytes": int(trace_cfg.get("maxBytes", 1_000_000)),
    }
    if cfg["secretPaths"] is None:
        cfg["secretPaths"] = list(load_rules("secrets.json").get("secretFiles", []))
    return cfg, warnings


# --------------------------------------------------------------------------
# Git and process helpers
# --------------------------------------------------------------------------


def git(root: str, *args: str, input_bytes: Optional[bytes] = None, timeout: int = 30) -> Tuple[int, bytes]:
    """Run git; returns (returncode, stdout). (-1, b"") if git is unavailable."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=root,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return -1, b""


def git_text(root: str, *args: str) -> Optional[str]:
    rc, out = git(root, *args)
    if rc != 0:
        return None
    return out.decode("utf-8", "replace").strip()


def find_repo_root(cwd: str) -> Optional[str]:
    if not os.path.isdir(cwd):
        return None
    top = git_text(cwd, "rev-parse", "--show-toplevel")
    return os.path.normpath(top) if top else None


def find_sh() -> Optional[str]:
    sh = shutil.which("sh")
    if sh or not IS_WINDOWS:
        return sh
    git_exe = shutil.which("git")
    candidates = [r"C:\Program Files\Git\bin\sh.exe", r"C:\Program Files (x86)\Git\bin\sh.exe"]
    if git_exe:
        gdir = os.path.dirname(git_exe)
        candidates[:0] = [os.path.join(gdir, "sh.exe"), os.path.join(gdir, "..", "bin", "sh.exe")]
    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.normpath(cand)
    return None


def _kill_tree(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def run_capture(argv: Any, cwd: str, timeout: float, shell: bool = False) -> Tuple[Optional[int], str]:
    """Run a command, merging stdout+stderr. Returns (rc or None on timeout, output).

    Runs in its own process group so a timeout kills grandchildren too.
    """
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env["CODING_PLUGIN_CHECK"] = "1"
    kwargs: Dict[str, Any] = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        shell=shell,
        env=env,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc: Optional[int] = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = b""
        rc = None
    return rc, (out or b"").decode("utf-8", "replace")


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def summarize(output: str, max_lines: int = 80, max_chars: int = 4000) -> str:
    text = ANSI_RE.sub("", output).replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    tail = lines[-max_lines:]
    body = "\n".join(tail)
    if len(body) > max_chars:
        body = "..." + body[-max_chars:]
    omitted = len(lines) - len(tail)
    prefix = f"[{omitted} earlier lines omitted]\n" if omitted > 0 else ""
    return prefix + body


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


class Ctx:
    """Per-invocation context: repo root, config, cached git facts."""

    def __init__(self, cwd: Optional[str] = None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        found = find_repo_root(self.cwd)
        self.is_git = found is not None
        self.root = found or self.cwd
        self.config, self.warnings = load_config(self.root)
        self._branch: Optional[str] = None
        self._branch_loaded = False
        self.trace_id = uuid.uuid4().hex[:8]
        self.session_id = ""
        self.trace_enabled = (
            os.environ.get("CODING_PLUGIN_TRACE") == "1" or bool(self.config["trace"]["enabled"])
        )

    def disabled(self, name: str) -> bool:
        env = os.environ.get("CODING_PLUGIN_DISABLE", "")
        names = {n.strip() for n in env.split(",") if n.strip()}
        names.update(str(n) for n in self.config.get("disabledGuards") or [])
        return "all" in names or name in names

    def log(self, msg: str, level: str = "INFO") -> None:
        log(self.root, msg, level)

    def trace(
        self,
        span: str,
        result: "Result",
        layer: str = "cli",
        event: str = "",
        rule_id: Optional[str] = None,
        parent_span: Optional[str] = None,
        duration_ms: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Append one record to .coding-plugin/log/trace.jsonl. No-op (and never raises) unless enabled."""
        if not self.trace_enabled:
            return
        outcome = "deny" if result.blocked else ("notice" if result.notice else "allow")
        record: Dict[str, Any] = {
            "trace_id": self.trace_id,
            "parent_id": f"{self.trace_id}:{parent_span}" if parent_span else None,
            "span": span,
            "layer": layer,
            "event": event,
            "session_id": self.session_id,
            "outcome": outcome,
            "rule_id": rule_id,
            "exit_code": result.code,
        }
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 2)
        record.update(extra)
        _trace_write(self.root, record, int(self.config["trace"]["maxBytes"]))

    @property
    def protected_branches(self) -> List[str]:
        return [str(b) for b in self.config.get("protectedBranches") or []]

    def current_branch(self) -> Optional[str]:
        if not self._branch_loaded:
            self._branch_loaded = True
            self._branch = git_text(self.root, "symbolic-ref", "--short", "-q", "HEAD") if self.is_git else None
        return self._branch


# --------------------------------------------------------------------------
# Paths and globs
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out += "(?:.*/)?"
                i += 3
                continue
            if pattern.startswith("**", i):
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + r"\Z", re.IGNORECASE)


def path_matches(target: str, patterns: Iterable[str]) -> bool:
    """Gitignore-style matching. Later patterns win; '!' negates.

    A pattern without '/' matches any path component; a pattern with '/'
    is anchored to the repo root. '**' crosses directories. Case-insensitive.
    """
    target = target.replace("\\", "/").strip("/")
    if not target:
        return False
    parts = target.split("/")
    matched = False
    for raw in patterns:
        pat = str(raw).strip()
        if not pat:
            continue
        neg = pat.startswith("!")
        if neg:
            pat = pat[1:]
        pat = pat.lstrip("/")
        if pat.endswith("/"):
            pat += "**"
        rx = _glob_regex(pat)
        if "/" in pat:
            hit = bool(rx.match(target))
        else:
            hit = any(rx.match(p) for p in parts)
        if hit:
            matched = not neg
    return matched


def norm_target(path: str, root: str, cwd: Optional[str] = None) -> Tuple[str, bool]:
    """Normalize a path for matching. Returns (path, inside_repo).

    Inside the repo the result is repo-relative with '/' separators;
    outside it is the absolute path with '/' separators.
    """
    p = path.strip().strip("\"'")
    if not p:
        return "", True
    if IS_WINDOWS:
        m = re.match(r"^/([a-zA-Z])(/.*)?$", p)  # Git Bash style /d/x -> D:/x
        if m:
            p = f"{m.group(1).upper()}:{m.group(2) or '/'}"
    p = os.path.expanduser(p)
    absp = p if os.path.isabs(p) else os.path.join(cwd or root, p)
    absp = os.path.normpath(absp)
    try:
        rel: Optional[str] = os.path.relpath(absp, root)
    except ValueError:  # different drive on Windows
        rel = None
    if rel is None or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return absp.replace("\\", "/"), False
    if rel == os.curdir:
        rel = ""
    return rel.replace("\\", "/"), True


def _samepath(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --------------------------------------------------------------------------
# guard-command
# --------------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|&?|(?<![<>&])&(?![>&])")
_WRAPPERS = {"sudo", "doas", "env", "command", "nohup", "time", "exec", "nice", "builtin", "xargs"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_segments(command: str) -> List[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(command) if s.strip()]


def _tokens(segment: str) -> List[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _program(tok: str) -> str:
    name = os.path.basename(tok.lstrip("\\(").replace("\\", "/"))
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def command_words(segment: str) -> List[str]:
    """Return [program, args...] with env assignments and wrappers removed."""
    toks = _tokens(segment)
    i = 0
    while i < len(toks):
        tok = toks[i]
        if _ASSIGN.match(tok) or tok in ("(", "{", "!"):
            i += 1
            continue
        if _program(tok) in _WRAPPERS:
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        break
    if i >= len(toks):
        return []
    return [_program(toks[i])] + toks[i + 1 :]


def _rule_rm(words: List[str], rule: Dict[str, Any], ctx: Ctx) -> Optional[str]:
    if not words or words[0] != "rm":
        return None
    recursive = False
    targets: List[str] = []
    end_opts = False
    for w in words[1:]:
        if not end_opts and w == "--":
            end_opts = True
        elif not end_opts and w.startswith("--"):
            recursive = recursive or w == "--recursive"
        elif not end_opts and w.startswith("-") and len(w) > 1:
            recursive = recursive or "r" in w or "R" in w
        else:
            targets.append(w)
    if not recursive:
        return None
    danger = set(rule.get("targets") or [])
    home = os.path.expanduser("~")
    for t in targets:
        stripped = t.rstrip("/\\") or t
        if t in danger or stripped in danger:
            return t
        expanded = os.path.expandvars(os.path.expanduser(t))
        if not os.path.isabs(expanded):
            expanded = os.path.join(ctx.cwd, expanded)
        expanded = os.path.normpath(expanded)
        if os.path.dirname(expanded) == expanded:  # filesystem root (/, C:\)
            return t
        if _samepath(expanded, home) or (ctx.is_git and _samepath(expanded, ctx.root)):
            return t
    return None


def _git_sub(words: List[str]) -> Tuple[Optional[str], List[str]]:
    if not words or words[0] != "git":
        return None, []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(words):
        return None, []
    return words[i], words[i + 1 :]


def _rule_force_push(words: List[str], rule: Dict[str, Any], ctx: Ctx) -> Optional[str]:
    sub, args = _git_sub(words)
    if sub != "push":
        return None
    force = False
    delete = False
    positional: List[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in ("--force", "-f") or a.startswith(("--force-with-lease", "--force-if-includes")):
            force = True
        elif a in ("--delete", "-d"):
            delete = True
        elif a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip_next = True
        elif a.startswith("-") and not a.startswith("--"):
            force = force or "f" in a[1:]
            delete = delete or "d" in a[1:]
        elif not a.startswith("-"):
            positional.append(a)
    refspecs = positional[1:]
    if any(r.startswith("+") for r in refspecs):
        force = True
    if any(r.startswith(":") for r in refspecs):
        delete = True
    if not (force or delete):
        return None
    if "--all" in args or "--mirror" in args or "--branches" in args:
        return "--all/--mirror"
    protected = ctx.protected_branches
    dests: List[str] = []
    for r in refspecs:
        r = r.lstrip("+")
        dst = r.split(":", 1)[1] if ":" in r else r
        dst = re.sub(r"^refs/heads/", "", dst)
        if dst == "HEAD" or not dst:
            dst = ctx.current_branch() or "HEAD"
        dests.append(dst)
    if not refspecs:
        dests.append(ctx.current_branch() or "")
    for d in dests:
        if d in protected:
            return d
    return None


def _rule_reset_hard(words: List[str], rule: Dict[str, Any], ctx: Ctx) -> Optional[str]:
    sub, args = _git_sub(words)
    if sub == "reset" and "--hard" in args:
        branch = ctx.current_branch()
        if branch and branch in ctx.protected_branches:
            return branch
    return None


def _rule_secret_read(words: List[str], rule: Dict[str, Any], ctx: Ctx) -> Optional[str]:
    if not words or words[0] not in set(rule.get("commands") or []):
        return None
    for a in words[1:]:
        if a.startswith("-") or a in ("<", ">", ">>", "2>"):
            continue
        tgt, _inside = norm_target(a, ctx.root, ctx.cwd)
        if tgt and path_matches(tgt, ctx.config["secretPaths"]):
            return a
    return None


_SEGMENT_RULES = {
    "rm-recursive": _rule_rm,
    "git-force-push": _rule_force_push,
    "git-reset-hard": _rule_reset_hard,
    "secret-read": _rule_secret_read,
}


def _block_message(rule: Dict[str, Any], matched: str) -> str:
    rid = rule.get("id", "custom")
    return (
        f"coding-plugin blocked this command (rule `{rid}`): {rule.get('message', 'blocked by policy.')}\n"
        f"Matched: {matched}\n"
        "If this is genuinely required, ask the user to run it themselves, or disable the rule with "
        f'"disabledGuards": ["{rid}"] in .coding-plugin.json.'
    )


def guard_command(ctx: Ctx, command: str) -> Result:
    if ctx.disabled("guard-command") or not command.strip():
        return Result()
    for pattern in ctx.config["commandRules"]["allow"]:
        try:
            if re.search(str(pattern), command):
                return Result()
        except re.error:
            ctx.log(f"invalid allow pattern {pattern!r}", "WARN")
    rules = list(load_rules("commands.json").get("rules", []))
    for extra in ctx.config["commandRules"]["extraDeny"]:
        if isinstance(extra, dict) and extra.get("pattern"):
            rules.append({"type": "regex", "id": extra.get("id", "custom"), **extra})
    segments = split_segments(command)
    words_list = [command_words(s) for s in segments]
    for rule in rules:
        rid = str(rule.get("id", "custom"))
        if ctx.disabled(rid) or (rule.get("guard") and ctx.disabled(str(rule["guard"]))):
            continue
        rtype = rule.get("type", "regex")
        matched: Optional[str] = None
        if rtype == "regex":
            try:
                m = re.search(str(rule.get("pattern", "")), command)
            except re.error:
                ctx.log(f"invalid pattern in rule {rid}", "WARN")
                continue
            matched = m.group(0) if m else None
        elif rtype in _SEGMENT_RULES:
            for words in words_list:
                matched = _SEGMENT_RULES[rtype](words, rule, ctx)
                if matched:
                    break
        if matched:
            ctx.log(f"guard-command deny rule={rid} command={command!r}")
            return Result(EXIT_BLOCK, _block_message(rule, matched))
    return Result()


# --------------------------------------------------------------------------
# guard-path
# --------------------------------------------------------------------------


def guard_path(ctx: Ctx, path: str, mode: str = "edit") -> Result:
    guard = "guard-path" if mode == "edit" else "read-secrets"
    if ctx.disabled(guard) or not path:
        return Result()
    target, _inside = norm_target(path, ctx.root, ctx.cwd)
    key = "protectedPaths" if mode == "edit" else "secretPaths"
    if not path_matches(target, ctx.config[key]):
        return Result()
    ctx.log(f"guard-path deny mode={mode} path={target!r}")
    if mode == "edit":
        msg = (
            f"coding-plugin blocked editing `{target}`: it matches protectedPaths in .coding-plugin.json. "
            "Explain the change you need and ask the user to make it, or to adjust protectedPaths."
        )
    else:
        msg = (
            f"coding-plugin blocked reading `{target}`: it looks like a secrets file (secretPaths). "
            "Ask the user for the specific non-secret value you need instead."
        )
    return Result(EXIT_BLOCK, msg)


# --------------------------------------------------------------------------
# format-file
# --------------------------------------------------------------------------

_PRETTIER_EXTS = {
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".json", ".css",
    ".scss", ".less", ".html", ".vue", ".md", ".mdx", ".yaml", ".yml", ".graphql",
}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", STATE_DIR}


def _default_formatters(ctx: Ctx, ext: str) -> List[List[str]]:
    if ext in (".py", ".pyi"):
        ruff = ["ruff", "format", "--quiet", "{file}"]
        black = ["black", "--quiet", "{file}"]
        try:
            with open(os.path.join(ctx.root, "pyproject.toml"), encoding="utf-8") as fh:
                py = fh.read()
        except OSError:
            py = ""
        if "[tool.black]" in py and "[tool.ruff" not in py:
            return [black, ruff]
        return [ruff, black]
    if ext in _PRETTIER_EXTS:
        return [["{root}/node_modules/.bin/prettier", "--write", "--log-level", "warn", "{file}"]]
    if ext == ".go":
        return [["gofmt", "-w", "{file}"]]
    if ext == ".rs":
        return [["rustfmt", "--edition", "2021", "{file}"]]
    return []


def _formatter_chain(ctx: Ctx, ext: str) -> List[List[str]]:
    override = (ctx.config.get("formatters") or {}).get(ext)
    if isinstance(override, list) and override:
        if all(isinstance(x, str) for x in override):
            return [override]
        return [x for x in override if isinstance(x, list) and x]
    return _default_formatters(ctx, ext)


def resolve_exe(name: str, root: str) -> Optional[str]:
    if "/" in name or "\\" in name:
        path = name if os.path.isabs(name) else os.path.join(root, name)
        exts = ["", ".cmd", ".exe", ".bat"] if IS_WINDOWS else [""]
        for e in exts:
            if os.path.isfile(path + e):
                return os.path.normpath(path + e)
        return None
    return shutil.which(name)


def format_file(ctx: Ctx, path: str) -> Result:
    if ctx.disabled("format-file") or not ctx.config.get("formatOnEdit", True) or not path:
        return Result()
    target, inside = norm_target(path, ctx.root, ctx.cwd)
    if not inside or not target or _SKIP_DIRS.intersection(target.split("/")):
        return Result()
    absp = os.path.join(ctx.root, target)
    if not os.path.isfile(absp):
        return Result()
    ext = os.path.splitext(absp)[1].lower()
    for template in _formatter_chain(ctx, ext):
        argv = [a.replace("{file}", absp).replace("{root}", ctx.root) for a in template]
        exe = resolve_exe(argv[0], ctx.root)
        if not exe:
            continue
        argv[0] = exe
        try:
            p = subprocess.run(argv, cwd=ctx.root, capture_output=True, timeout=10)
            if p.returncode != 0:
                err = p.stderr.decode("utf-8", "replace").strip()[:300]
                ctx.log(f"format-file: {os.path.basename(exe)} exited {p.returncode} on {target}: {err}", "WARN")
        except (OSError, subprocess.SubprocessError) as exc:
            ctx.log(f"format-file: {os.path.basename(exe)} failed on {target}: {exc}", "WARN")
        return Result()
    return Result()


# --------------------------------------------------------------------------
# run-checks
# --------------------------------------------------------------------------


def resolve_check_command(ctx: Ctx) -> Optional[str]:
    configured = ctx.config.get("checkCommand")
    if configured:
        return str(configured)
    for cand in ("scripts/check", "scripts/check.sh", "scripts/check.py"):
        if os.path.isfile(os.path.join(ctx.root, cand)):
            return cand
    try:
        with open(os.path.join(ctx.root, "Makefile"), encoding="utf-8", errors="replace") as fh:
            if re.search(r"^check\s*:", fh.read(), re.MULTILINE):
                return "make check"
    except OSError:
        pass
    return None


def check_argv(ctx: Ctx, cmd: str) -> Tuple[Any, bool]:
    """Turn a check command into (argv, shell) for subprocess."""
    toks = _tokens(cmd)
    if toks:
        first = toks[0]
        fpath = first if os.path.isabs(first) else os.path.join(ctx.root, first)
        if os.path.isfile(fpath):
            ext = os.path.splitext(fpath)[1].lower()
            rest = toks[1:]
            if ext == ".py":
                return [sys.executable, fpath, *rest], False
            if ext == ".ps1":
                return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", fpath, *rest], False
            if ext in (".cmd", ".bat", ".exe"):
                return [fpath, *rest], False
            if not IS_WINDOWS and os.access(fpath, os.X_OK):
                return [fpath, *rest], False
            sh = find_sh()
            if sh:
                return [sh, fpath, *rest], False
    sh = find_sh()
    if sh:
        return [sh, "-c", cmd], False
    return cmd, True


def execute_check(ctx: Ctx, cmd: str, timeout: Optional[float] = None) -> Tuple[Optional[int], str, float]:
    argv, shell = check_argv(ctx, cmd)
    start = time.time()
    rc, out = run_capture(argv, ctx.root, timeout or ctx.config["checkTimeoutSec"], shell=shell)
    return rc, out, time.time() - start


def fingerprint(ctx: Ctx) -> Tuple[str, bool]:
    """Hash of HEAD + staged + unstaged + untracked state. Returns (hash, clean)."""
    h = hashlib.sha256()
    excl = ":(exclude)" + STATE_DIR
    _, head = git(ctx.root, "rev-parse", "-q", "--verify", "HEAD")
    h.update(head)
    clean = True
    for args in (
        ("diff", "--binary", "--no-color", "--no-ext-diff"),
        ("diff", "--cached", "--binary", "--no-color", "--no-ext-diff"),
    ):
        _, out = git(ctx.root, *args, "--", ".", excl)
        clean = clean and not out.strip()
        h.update(b"\0" + out)
    _, out = git(ctx.root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", excl)
    for raw in out.split(b"\0"):
        if not raw:
            continue
        clean = False
        try:
            st = os.stat(os.path.join(ctx.root, raw.decode("utf-8", "surrogateescape")))
            h.update(raw + f":{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(raw)
    return h.hexdigest(), clean


def _session_state(state: Dict[str, Any], session: str) -> Dict[str, Any]:
    sessions = state.setdefault("sessions", {})
    s = sessions.setdefault(session or "default", {})
    s["t"] = time.time()
    if len(sessions) > 20:
        for old in sorted(sessions, key=lambda k: sessions[k].get("t", 0))[: len(sessions) - 20]:
            sessions.pop(old, None)
    return s


def run_checks(ctx: Ctx, session: str = "", continuing: bool = False, force: bool = False) -> Result:
    """Stop-time gate: run the check command if the tree changed; retry-capped."""
    if ctx.disabled("run-checks"):
        return Result()
    if not force and not ctx.config.get("requireChecksOnStop", True):
        return Result()
    if not force and not ctx.is_git:
        ctx.log("run-checks: not a git repository; skipped")
        return Result()
    cmd = resolve_check_command(ctx)
    if not cmd:
        ctx.log("run-checks: no check command configured; skipped")
        return Result()
    state = load_state(ctx.root, "run-checks")
    sess = _session_state(state, session)
    if not continuing:
        sess["attempts"] = 0
    fp, clean = fingerprint(ctx) if ctx.is_git else ("", False)
    if not force:
        if fp and fp == state.get("lastGreen"):
            save_state(ctx.root, "run-checks", state)
            return Result()
        baseline = sess.get("baseline")
        if (baseline is not None and fp == baseline) or (baseline is None and clean):
            save_state(ctx.root, "run-checks", state)
            return Result()
    rc, output, elapsed = execute_check(ctx, cmd)
    if rc is None:
        save_state(ctx.root, "run-checks", state)
        ctx.log(f"run-checks: `{cmd}` timed out after {elapsed:.0f}s", "WARN")
        return Result(notice=f"coding-plugin: `{cmd}` timed out after {elapsed:.0f}s; checks were not enforced.")
    if rc == 0:
        state["lastGreen"] = fp
        sess["attempts"] = 0
        save_state(ctx.root, "run-checks", state)
        ctx.log(f"run-checks: `{cmd}` passed in {elapsed:.1f}s")
        return Result()
    attempts = int(sess.get("attempts", 0))
    cap = int(ctx.config.get("stopRetryCap", 3))
    ctx.log(f"run-checks: `{cmd}` failed (exit {rc}), attempt {attempts + 1}/{cap}")
    if attempts >= cap:
        save_state(ctx.root, "run-checks", state)
        return Result(
            notice=(
                f"coding-plugin: checks are still failing after {cap} attempts; letting the agent stop. "
                f"Run `{cmd}` to see the failures."
            )
        )
    sess["attempts"] = attempts + 1
    save_state(ctx.root, "run-checks", state)
    msg = (
        f"coding-plugin: the project checks failed (`{cmd}` exited {rc}; attempt {attempts + 1} of {cap}).\n"
        "Fix the failures below, then finish again. Do not disable or skip checks to get past this.\n"
        "--- check output (tail) ---\n" + summarize(output)
    )
    return Result(EXIT_BLOCK, msg)


# --------------------------------------------------------------------------
# session-context
# --------------------------------------------------------------------------


def _read_version(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            m = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.MULTILINE)
        return m.group(1) if m else None
    except OSError:
        return None


def session_context(ctx: Ctx, session: str = "") -> Result:
    lines = [f"[coding-plugin {__version__}]"]
    if not ctx.is_git:
        lines.append("Not a git repository: change detection and git-based guards are inactive.")
        return Result(context="\n".join(lines))
    branch = ctx.current_branch()
    _, porcelain = git(ctx.root, "status", "--porcelain")
    dirty = len([ln for ln in porcelain.decode("utf-8", "replace").splitlines() if ln.strip()])
    lines.append(f"Branch: {branch or '(detached HEAD)'}; {dirty} uncommitted change(s).")
    if branch and branch in ctx.protected_branches:
        lines.append(
            f"WARNING: you are on protected branch '{branch}'. Create a feature branch before changing code "
            "(git switch -c <topic>)."
        )
    cmd = resolve_check_command(ctx)
    if cmd:
        lines.append(f"Check command: `{cmd}`. Run it and make it pass before declaring work done.")
    else:
        lines.append("No check command found: add scripts/check or set checkCommand in .coding-plugin.json.")
    for w in ctx.warnings:
        lines.append(f"Config warning: {w}")
    vendored = os.path.join(ctx.root, STATE_DIR, "bin", "coding_plugin.py")
    if os.path.isfile(vendored) and not _samepath(vendored, os.path.abspath(__file__)):
        v = _read_version(vendored)
        if v and v != __version__:
            lines.append(
                f"Note: this project vendors coding-plugin core {v}, but the agent plugin is {__version__}. "
                "Re-run the init skill to update the git hooks."
            )
    if not ctx.disabled("run-checks"):
        try:
            state = load_state(ctx.root, "run-checks")
            _session_state(state, session)["baseline"] = fingerprint(ctx)[0]
            save_state(ctx.root, "run-checks", state)
        except Exception as exc:
            ctx.log(f"session-context: could not record baseline: {exc}", "WARN")
    return Result(context="\n".join(lines))


# --------------------------------------------------------------------------
# Secret scanning
# --------------------------------------------------------------------------


def _secret_patterns() -> Tuple[List[Tuple[str, "re.Pattern[str]"]], str, List[str]]:
    rules = load_rules("secrets.json")
    pats = []
    for r in rules.get("patterns", []):
        try:
            pats.append((str(r["id"]), re.compile(r["regex"])))
        except (KeyError, re.error):
            continue
    return pats, str(rules.get("allowMarker", "coding-plugin:allow-secret")), list(rules.get("allowValues", []))


def _scan_line(line: str, pats: List[Tuple[str, "re.Pattern[str]"]], marker: str, allow: List[str]) -> Optional[str]:
    if marker in line:
        return None
    for rid, rx in pats:
        for m in rx.finditer(line):
            if m.group(0) not in allow:
                return rid
    return None


def _git_names(ctx: Ctx, *args: str) -> List[str]:
    _, out = git(ctx.root, *args, "-z")
    return [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]


def _gitleaks(ctx: Ctx, staged: bool) -> List[str]:
    exe = shutil.which("gitleaks")
    if not exe:
        return []
    common = ["--redact", "--no-banner", "--exit-code", "3"]
    attempts = (
        [[exe, "git", "--pre-commit", "--staged", *common], [exe, "protect", "--staged", *common]]
        if staged
        else [[exe, "dir", ".", *common], [exe, "detect", "--no-git", *common]]
    )
    for argv in attempts:
        try:
            p = subprocess.run(argv, cwd=ctx.root, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            ctx.log(f"gitleaks failed to run: {exc}", "WARN")
            return []
        if p.returncode == 0:
            return []
        if p.returncode == 3:
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            return ["gitleaks reported possible leaks:\n" + summarize(out, 30, 2000)]
    ctx.log("gitleaks returned an unexpected status; ignored", "WARN")
    return []


def scan_secrets(ctx: Ctx, staged: bool = True) -> Result:
    if ctx.disabled("secret-scan") or not ctx.is_git:
        return Result()
    pats, marker, allow = _secret_patterns()
    findings: List[str] = []
    secret_globs = ctx.config["secretPaths"]
    if staged:
        for name in _git_names(ctx, "diff", "--cached", "--name-only", "--diff-filter=ACMR"):
            if path_matches(name, secret_globs):
                findings.append(f"{name}: secrets file staged for commit")
        _, diff = git(ctx.root, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR")
        current, lineno = "", 0
        for line in diff.decode("utf-8", "replace").splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                lineno = int(m.group(1)) if m else 0
            elif line.startswith("+"):
                rid = _scan_line(line[1:], pats, marker, allow)
                if rid:
                    findings.append(f"{current}:{lineno}: {rid}")
                lineno += 1
    else:
        for name in _git_names(ctx, "ls-files"):
            if path_matches(name, secret_globs):
                findings.append(f"{name}: secrets file is tracked by git")
                continue
            path = os.path.join(ctx.root, name)
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"\0" in data[:8000]:
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                rid = _scan_line(line, pats, marker, allow)
                if rid:
                    findings.append(f"{name}:{i}: {rid}")
    findings.extend(_gitleaks(ctx, staged))
    if not findings:
        return Result()
    shown = findings[:50]
    more = f"\n  ... and {len(findings) - 50} more" if len(findings) > 50 else ""
    ctx.log(f"scan-secrets: {len(findings)} finding(s)")
    return Result(
        EXIT_BLOCK,
        "coding-plugin secret scan found possible secrets (values not shown):\n"
        + "\n".join(f"  - {f}" for f in shown)
        + more
        + f"\nRemove them (and rotate any real credential). For a false positive, add `{marker}` "
        "in a comment on that line.",
    )


# --------------------------------------------------------------------------
# Git hook entry points and CI
# --------------------------------------------------------------------------

_BYPASS_HINT = (
    "To bypass deliberately: CODING_PLUGIN_DISABLE=<guard> git <command> ... "
    f"(guards: {', '.join(GUARDS)}), or --no-verify."
)


def pre_commit(ctx: Ctx) -> Result:
    msgs: List[str] = []
    t0 = time.monotonic()
    r = scan_secrets(ctx, staged=True)
    ctx.trace("scan_secrets", r, layer="git-hook", event="pre-commit", parent_span="pre_commit",
              duration_ms=(time.monotonic() - t0) * 1000)
    if r.blocked:
        msgs.append(r.message)
    if not ctx.disabled("guard-path"):
        hits = [
            n
            for n in _git_names(ctx, "diff", "--cached", "--name-only", "--diff-filter=ACMRD")
            if path_matches(n, ctx.config["protectedPaths"])
        ]
        if hits:
            msgs.append(
                "coding-plugin: commit touches protected paths (protectedPaths in .coding-plugin.json):\n"
                + "\n".join(f"  - {h}" for h in hits)
            )
    if ctx.config.get("preCommitCheck") and not ctx.disabled("run-checks"):
        cmd = resolve_check_command(ctx)
        if cmd:
            t1 = time.monotonic()
            rc, out, _ = execute_check(ctx, cmd)
            ctx.trace("run_checks", Result(EXIT_BLOCK if rc != 0 else 0), layer="git-hook", event="pre-commit",
                      parent_span="pre_commit", duration_ms=(time.monotonic() - t1) * 1000, check_rc=rc)
            if rc != 0:
                status = "timed out" if rc is None else f"exited {rc}"
                msgs.append(f"coding-plugin: `{cmd}` {status}:\n{summarize(out)}")
    if msgs:
        return Result(EXIT_BLOCK, "\n\n".join(msgs) + "\n\n" + _BYPASS_HINT)
    return Result()


_ZERO_SHA = re.compile(r"^0+$")


def pre_push(ctx: Ctx, stdin_text: str) -> Result:
    msgs: List[str] = []
    pushing = False
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _lref, lsha, rref, rsha = parts
        if not _ZERO_SHA.match(lsha):
            pushing = True
        if not rref.startswith("refs/heads/") or ctx.disabled("force-push"):
            continue
        branch = rref[len("refs/heads/") :]
        if branch not in ctx.protected_branches:
            continue
        if _ZERO_SHA.match(lsha):
            msgs.append(f"coding-plugin: deleting protected branch '{branch}' is blocked.")
        elif not _ZERO_SHA.match(rsha):
            rc, _ = git(ctx.root, "merge-base", "--is-ancestor", rsha, lsha)
            if rc != 0:
                msgs.append(
                    f"coding-plugin: non-fast-forward (force) push to protected branch '{branch}' is blocked. "
                    "If the remote moved, fetch and rebase first."
                )
    if not msgs and pushing and not ctx.disabled("run-checks"):
        cmd = resolve_check_command(ctx)
        if cmd:
            t1 = time.monotonic()
            rc, out, _ = execute_check(ctx, cmd)
            ctx.trace("run_checks", Result(EXIT_BLOCK if rc != 0 else 0), layer="git-hook", event="pre-push",
                      parent_span="pre_push", duration_ms=(time.monotonic() - t1) * 1000, check_rc=rc)
            if rc != 0:
                status = "timed out" if rc is None else f"exited {rc}"
                msgs.append(f"coding-plugin: `{cmd}` {status}; push blocked.\n{summarize(out)}")
    if msgs:
        return Result(EXIT_BLOCK, "\n\n".join(msgs) + "\n\n" + _BYPASS_HINT)
    return Result()


def ci(ctx: Ctx) -> Result:
    msgs: List[str] = []
    t0 = time.monotonic()
    r = scan_secrets(ctx, staged=False)
    ctx.trace("scan_secrets", r, layer="ci", event="ci", parent_span="ci", duration_ms=(time.monotonic() - t0) * 1000)
    if r.blocked:
        msgs.append(r.message)
    cmd = resolve_check_command(ctx)
    if not cmd:
        msgs.append("coding-plugin: no check command found (add scripts/check or set checkCommand).")
    elif not ctx.disabled("run-checks"):
        t1 = time.monotonic()
        rc, out, elapsed = execute_check(ctx, cmd)
        ctx.trace("run_checks", Result(EXIT_BLOCK if rc != 0 else 0), layer="ci", event="ci",
                  parent_span="ci", duration_ms=(time.monotonic() - t1) * 1000, check_rc=rc)
        sys.stdout.write(out)
        if rc != 0:
            status = "timed out" if rc is None else f"exited {rc}"
            msgs.append(f"coding-plugin: `{cmd}` {status} after {elapsed:.0f}s.")
    if msgs:
        return Result(EXIT_BLOCK, "\n\n".join(msgs))
    return Result(context="coding-plugin: all checks passed.")


# --------------------------------------------------------------------------
# install (vendoring + templates), used by the init skill
# --------------------------------------------------------------------------

STACKS = ("python", "node", "go", "rust", "none")
KNOWN_AGENTS = ("claude-code", "codex", "cursor", "gemini", "copilot")
_CI_PLACEHOLDER = "      # {{stack-setup}}\n"
_CI_STACK_SETUP = {
    "python": (
        "      - name: Install dependencies\n"
        "        run: |\n"
        "          python -m pip install --upgrade pip\n"
        "          if [ -f requirements-dev.txt ]; then pip install -r requirements-dev.txt; fi\n"
        "          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi\n"
        "          if [ -f pyproject.toml ]; then pip install -e . 2>/dev/null || true; fi\n"
    ),
    "node": (
        "      - uses: actions/setup-node@v4\n"
        "        with:\n"
        "          node-version: lts/*\n"
        "      - name: Install dependencies\n"
        "        run: npm ci\n"
    ),
    "go": (
        "      - uses: actions/setup-go@v5\n"
        "        with:\n"
        "          go-version-file: go.mod\n"
    ),
    "rust": "      - name: Rust components\n        run: rustup component add rustfmt clippy\n",
    "none": "",
}

# ---- CI systems -------------------------------------------------------------
CI_SYSTEMS = ("github", "gitlab", "bitbucket", "azure")
CI_CHOICES = ("auto",) + CI_SYSTEMS + ("none",)
CI_FILES = {
    "github": ".github/workflows/coding-plugin.yml",
    "gitlab": ".gitlab-ci.yml",
    "bitbucket": "bitbucket-pipelines.yml",
    "azure": "azure-pipelines.yml",
}
CI_RUN = "python3 .coding-plugin/bin/coding_plugin.py ci"
_CI_IMAGE = {"python": "python:3.12", "node": "node:lts", "go": "golang:1", "rust": "rust:1", "none": "python:3.12"}
_PY_DEPS = [
    "python3 -m pip install --upgrade pip",
    "if [ -f requirements-dev.txt ]; then python3 -m pip install -r requirements-dev.txt; fi",
    "if [ -f requirements.txt ]; then python3 -m pip install -r requirements.txt; fi",
    "if [ -f pyproject.toml ]; then python3 -m pip install -e . || true; fi",
]
_APT_PYTHON = "apt-get update -qq && apt-get install -y -qq python3 > /dev/null"
# stack -> (setup needed only in container images, setup needed everywhere)
_CI_SETUP: Dict[str, Tuple[List[str], List[str]]] = {
    "python": ([], _PY_DEPS),
    "node": ([], ["npm ci"]),
    "go": ([_APT_PYTHON], []),
    "rust": ([_APT_PYTHON], ["rustup component add rustfmt clippy"]),
    "none": ([], []),
}
# One step to add to an existing pipeline, per CI system.
_CI_STEP = {
    "github": "- name: coding-plugin (secret scan + checks)\n  run: {cmd}",
    "gitlab": "script:\n  - {cmd}",
    "bitbucket": "script:\n  - {cmd}",
    "azure": "- script: {cmd}\n  displayName: coding-plugin (secret scan + checks)",
    "circleci": "- run: {cmd}",
    "jenkins": "sh '{cmd}'",
}


def detect_ci(target: str) -> List[Tuple[str, str]]:
    """Existing CI configuration in a project as (system, repo-relative path); ignores our own workflow."""
    found: List[Tuple[str, str]] = []
    wf = os.path.join(target, ".github", "workflows")
    if os.path.isdir(wf):
        for name in sorted(os.listdir(wf)):
            if name.endswith((".yml", ".yaml")) and name != os.path.basename(CI_FILES["github"]):
                found.append(("github", f".github/workflows/{name}"))
    for system, rel in (
        ("gitlab", ".gitlab-ci.yml"),
        ("bitbucket", "bitbucket-pipelines.yml"),
        ("azure", "azure-pipelines.yml"),
        ("azure", ".azure-pipelines"),
        ("jenkins", "Jenkinsfile"),
        ("circleci", ".circleci/config.yml"),
    ):
        if os.path.exists(os.path.join(target, *rel.split("/"))):
            found.append((system, rel))
    return found


def infer_ci_from_remote(target: str) -> str:
    url = (git_text(target, "remote", "get-url", "origin") or "").lower()
    for marker, system in (("gitlab", "gitlab"), ("bitbucket", "bitbucket"),
                           ("dev.azure.com", "azure"), ("visualstudio.com", "azure")):
        if marker in url:
            return system
    return "github"


def ci_step_note(system: str, path: str) -> str:
    step = _CI_STEP[system].format(cmd=CI_RUN)
    indented = "\n".join("      " + ln for ln in step.splitlines())
    return (
        f"existing {system} CI in {path}: add this step after your dependency install "
        "(if that pipeline already runs your tests, add only "
        "`python3 .coding-plugin/bin/coding_plugin.py scan-secrets --all`):\n" + indented
    )


def _render_ci(template: bytes, image: str, commands: List[str], fmt: str) -> bytes:
    """Replace '{{image}}' and expand a '# {{setup}}' line into one item per command."""
    out: List[str] = []
    for line in template.decode("utf-8").splitlines(keepends=True):
        if line.strip() == "# {{setup}}":
            indent = line[: len(line) - len(line.lstrip())]
            out += [indent + fmt.format(cmd=json.dumps(c)) + "\n" for c in commands]
        else:
            out.append(line)
    return "".join(out).replace("{{image}}", image).encode("utf-8")


def ci_file_content(system: str, stack: str, tdir: str) -> bytes:
    def tmpl(name: str) -> bytes:
        return _lf(_read_bytes(os.path.join(tdir, "ci", name)))

    container_only, common = _CI_SETUP[stack]
    if system == "github":
        return tmpl("github-actions.yml").replace(_CI_PLACEHOLDER.encode(), _CI_STACK_SETUP[stack].encode())
    if system in ("gitlab", "bitbucket"):
        name = "gitlab-ci.yml" if system == "gitlab" else "bitbucket-pipelines.yml"
        return _render_ci(tmpl(name), _CI_IMAGE[stack], container_only + common, "- {cmd}")
    if system == "azure":
        return _render_ci(tmpl("azure-pipelines.yml"), "", common, "- script: {cmd}")
    raise ValueError(system)


def _plan_ci(target: str, stack: str, ci: str, tdir: str
             ) -> Tuple[List[Tuple[str, bytes, str, bool]], List[str], List[str]]:
    """Decide which CI file (if any) to create. Returns (plan entries, report lines, notes)."""
    existing = detect_ci(target)
    report = [f"detected   {rel}  ({system} CI)" for system, rel in existing]
    notes: List[str] = []
    seen_systems = set()
    step_notes = []
    for system, rel in existing:
        if system not in seen_systems:
            seen_systems.add(system)
            step_notes.append(ci_step_note(system, rel))
    system = ci
    if ci == "auto":
        if os.path.isfile(os.path.join(target, *CI_FILES["github"].split("/"))):
            system = "github"  # we created it earlier; keep managing it
        elif existing:
            notes.append("existing CI found, so no pipeline file was created (re-run with --ci <system> "
                         "to add a separate one).")
            return [], report, notes + step_notes
        else:
            system = infer_ci_from_remote(target)
    if system == "none":
        return [], report, (step_notes if existing else ["no CI configured (--ci none)."])
    rel = CI_FILES[system]
    if system == "github" and "github" in seen_systems:
        notes.append(f"{rel} runs alongside your existing workflows. To avoid running the tests twice, "
                     "consider adding the step to an existing workflow instead and re-running with --ci none.")
        notes += [n for n in step_notes if n.startswith("existing github")]
    elif os.path.isfile(os.path.join(target, *rel.split("/"))):
        notes += [n for n in step_notes if f" in {rel}:" in n]
    return [(rel, ci_file_content(system, stack, tdir), "ci", False)], report, notes


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def install(target: str, stack: str, agents: List[str], dry_run: bool, overwrite: Iterable[str] = (),
            ci: str = "auto") -> Result:
    """Vendor the core and copy templates into a project, never silently overwriting.

    Policies: managed = toolkit-owned, always updated; user = created once,
    reported as a conflict if it exists with different content; ci = like user,
    but an existing pipeline is reported as 'existing' (never offered for
    overwrite); lines = the listed lines are appended if missing.
    """
    tdir = find_resource_dir("templates")
    rdir = find_resource_dir("rules")
    if not tdir or not rdir:
        return Result(EXIT_BLOCK, "coding-plugin install: templates/ or rules/ not found next to this script.")
    if stack not in STACKS:
        return Result(EXIT_BLOCK, f"coding-plugin install: unknown stack {stack!r} (choose from {', '.join(STACKS)}).")
    if ci not in CI_CHOICES:
        return Result(EXIT_BLOCK, f"coding-plugin install: unknown CI {ci!r} (choose from {', '.join(CI_CHOICES)}).")
    target = os.path.abspath(target)
    overwrite = {o.replace("\\", "/") for o in overwrite}
    notes: List[str] = []
    plan: List[Tuple[str, bytes, str, bool]] = [
        (f"{STATE_DIR}/bin/coding_plugin.py", _lf(_read_bytes(os.path.abspath(__file__))), "managed", True),
        (f"{STATE_DIR}/.gitignore", STATE_GITIGNORE.encode(), "managed", False),
    ]
    for name in sorted(os.listdir(rdir)):
        if name.endswith(".json"):
            plan.append((f"{STATE_DIR}/bin/rules/{name}", _lf(_read_bytes(os.path.join(rdir, name))), "managed", False))

    def tmpl(*parts: str) -> bytes:
        return _lf(_read_bytes(os.path.join(tdir, *parts)))

    plan += [
        (CONFIG_FILE, tmpl("coding-plugin.json"), "user", False),
        (".githooks/pre-commit", tmpl("githooks", "pre-commit"), "user", True),
        (".githooks/pre-push", tmpl("githooks", "pre-push"), "user", True),
        ("AGENTS.md", tmpl("AGENTS.md.tmpl"), "user", False),
        (".gitattributes", b".githooks/* text eol=lf\nscripts/check text eol=lf\n", "lines", False),
    ]
    if stack != "none":
        plan.append(("scripts/check", tmpl("check", f"{stack}.sh"), "user", True))
    ci_entries, ci_report, ci_notes = _plan_ci(target, stack, ci, tdir)
    plan += ci_entries
    for agent in agents:
        if agent not in KNOWN_AGENTS:
            notes.append(f"unknown agent {agent!r} ignored (known: {', '.join(KNOWN_AGENTS)}).")
    if "claude-code" in agents:
        claude_md = os.path.join(target, "CLAUDE.md")
        if os.path.isfile(claude_md) and "@AGENTS.md" in _read_bytes(claude_md).decode("utf-8", "replace"):
            pass  # already imports AGENTS.md
        else:
            plan.append(("CLAUDE.md", tmpl("CLAUDE.md.tmpl"), "user", False))
        notes.append("Claude Code: install the plugin with /plugin marketplace add arthurcyf/coding-plugin "
                     "then /plugin install coding-plugin@coding-plugin.")
    if "gemini" in agents:
        notes.append('Gemini CLI: add {"context": {"fileName": ["AGENTS.md"]}} to .gemini/settings.json '
                     "so it reads AGENTS.md.")
    for agent in ("codex", "cursor", "copilot"):
        if agent in agents:
            notes.append(f"{agent}: reads AGENTS.md natively; git hooks and CI enforce the checks.")

    notes += ci_notes
    report: List[str] = list(ci_report)
    writes: List[Tuple[str, bytes, bool]] = []
    for rel, content, policy, executable in plan:
        dest = os.path.join(target, *rel.split("/"))
        exists = os.path.isfile(dest)
        current = _read_bytes(dest) if exists else b""
        if policy == "lines":
            have = set(current.decode("utf-8", "replace").splitlines())
            missing = [ln for ln in content.decode().splitlines() if ln not in have]
            if not missing:
                report.append(f"unchanged  {rel}")
                continue
            sep = b"" if not current or current.endswith(b"\n") else b"\n"
            new = current + sep + ("\n".join(missing) + "\n").encode()
            report.append(f"{'append' if exists else 'create'}     {rel}")
            writes.append((dest, new, executable))
        elif not exists:
            report.append(f"create     {rel}")
            writes.append((dest, content, executable))
        elif _lf(current) == content:
            report.append(f"unchanged  {rel}")
        elif policy == "managed":
            report.append(f"update     {rel}")
            writes.append((dest, content, executable))
        elif rel in overwrite:
            report.append(f"overwrite  {rel}")
            writes.append((dest, content, executable))
        elif policy == "ci":
            report.append(f"existing   {rel}  (your pipeline is left unchanged; see the note below)")
        else:
            report.append(f"conflict   {rel}  (exists with different content; pass --overwrite {rel} to replace)")
    if not dry_run:
        for dest, content, executable in writes:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(content)
            if executable and not IS_WINDOWS:
                os.chmod(dest, 0o755)
        if find_repo_root(target):
            git(target, "config", "core.hooksPath", ".githooks")
            if IS_WINDOWS:  # record the executable bit in the index for POSIX checkouts
                for rel, _c, _p, executable in plan:
                    if executable and os.path.isfile(os.path.join(target, rel)):
                        git(target, "update-index", "--add", "--chmod=+x", rel)
            report.append("git        core.hooksPath = .githooks")
        else:
            notes.append("not a git repository: run `git init` and `git config core.hooksPath .githooks`.")
    header = "coding-plugin install (dry run, nothing written)" if dry_run else "coding-plugin install"
    text = "\n".join([header + f" -> {target}", *report] + [f"note: {n}" for n in notes])
    return Result(context=text)


# --------------------------------------------------------------------------
# Normalized event dispatch (adapter entry point)
# --------------------------------------------------------------------------


def _handle(event: Dict[str, Any]) -> Result:
    ctx = Ctx(event.get("cwd") or None)
    ctx.session_id = str(event.get("session_id") or "")
    kind = event.get("event")
    t0 = time.monotonic()

    def done(span: str, result: Result, **extra: Any) -> Result:
        ctx.trace(span, result, layer="claude-code", event=str(kind or ""),
                  duration_ms=(time.monotonic() - t0) * 1000, **extra)
        return result

    if kind == "pre_command":
        command = str(event.get("command") or "")
        result = guard_command(ctx, command)
        return done("guard_command", result, rule_id=_trace_rule_id(result.message),
                    cmd_hash=_trace_hash(command), cmd_len=len(command))
    if kind == "pre_edit":
        path = str(event.get("path") or "")
        return done("guard_path", guard_path(ctx, path, "edit"), path=_trace_path(path), mode="edit")
    if kind == "pre_read":
        path = str(event.get("path") or "")
        return done("guard_path", guard_path(ctx, path, "read"), path=_trace_path(path), mode="read")
    if kind == "post_edit":
        path = str(event.get("path") or "")
        return done("format_file", format_file(ctx, path), path=_trace_path(path))
    if kind == "stop":
        result = run_checks(ctx, str(event.get("session_id") or ""), bool(event.get("continuing")))
        return done("run_checks", result)
    if kind == "session_start":
        return done("session_context", session_context(ctx, str(event.get("session_id") or "")))
    ctx.log(f"unknown event {kind!r}", "WARN")
    return Result()


def handle(event: Dict[str, Any]) -> Result:
    """Process a normalized event. Fails open on internal errors."""
    try:
        return _handle(event)
    except Exception:
        if strict():
            raise
        cwd = event.get("cwd") if isinstance(event, dict) else None
        log(find_repo_root(cwd or os.getcwd()) or cwd, "internal error:\n" + traceback.format_exc(), "ERROR")
        return Result()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def emit(res: Result) -> int:
    if res.context:
        sys.stdout.write(res.context.rstrip("\n") + "\n")
    if res.message:
        sys.stderr.write(res.message.rstrip("\n") + "\n")
    if res.notice:
        sys.stderr.write(res.notice.rstrip("\n") + "\n")
    sys.stdout.flush()
    sys.stderr.flush()
    return res.code


def _setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def read_stdin_text() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    except Exception:
        return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coding_plugin.py", description="coding-plugin core CLI")
    p.add_argument("--cwd", default=None, help="working directory (default: current)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("guard-command", help="allow/deny a shell command")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("guard-path", help="allow/deny editing or reading a path")
    s.add_argument("path")
    s.add_argument("--mode", choices=("edit", "read"), default="edit")
    s = sub.add_parser("format-file", help="format a file if a formatter is installed")
    s.add_argument("path")
    s = sub.add_parser("run-checks", help="run the check command (stop-time gate)")
    s.add_argument("--session", default="")
    s.add_argument("--continuing", action="store_true")
    s.add_argument("--force", action="store_true", help="run even if nothing changed")
    s = sub.add_parser("session-context", help="print branch / dirty state / warnings")
    s.add_argument("--session", default="")
    s = sub.add_parser("scan-secrets", help="scan staged changes or all tracked files for secrets")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", default=True)
    g.add_argument("--all", action="store_true")
    sub.add_parser("pre-commit", help="git pre-commit entry point")
    s = sub.add_parser("pre-push", help="git pre-push entry point (refs on stdin)")
    s.add_argument("remote", nargs="?")
    s.add_argument("url", nargs="?")
    sub.add_parser("ci", help="CI entry point: full secret scan + check command")
    s = sub.add_parser("install", help="vendor the core and templates into a project")
    s.add_argument("--target", default=".")
    s.add_argument("--stack", default="none", choices=STACKS)
    s.add_argument("--agents", default="", help="comma-separated: " + ",".join(KNOWN_AGENTS))
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--overwrite", action="append", default=[], help="repo-relative path to replace")
    s.add_argument("--ci", default="auto", choices=CI_CHOICES,
                   help="CI pipeline to generate. auto: none if a pipeline already exists (prints the step to "
                        "add), otherwise chosen from the git remote (default github)")
    sub.add_parser("hook", help="process a normalized event from stdin")
    sub.add_parser("version", help="print the version")
    s = sub.add_parser("trace", help="inspect .coding-plugin/log/trace.jsonl")
    tsub = s.add_subparsers(dest="trace_cmd", required=True)
    t = tsub.add_parser("tail", help="print the last N trace records")
    t.add_argument("--lines", type=int, default=20)
    t = tsub.add_parser("show", help="print trace records matching a trace id or session id")
    t.add_argument("--trace-id", default=None)
    t.add_argument("--session-id", default=None)
    return p


def trace_show(root: str, trace_id: Optional[str] = None, session_id: Optional[str] = None,
                lines: Optional[int] = None) -> Result:
    """Read .coding-plugin/log/trace.jsonl, optionally filtered, for `coding_plugin.py trace`."""
    path = os.path.join(root, STATE_DIR, "log", "trace.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [ln.rstrip("\n") for ln in fh if ln.strip()]
    except OSError:
        return Result(context="(no trace records yet; enable tracing first -- see README.)")
    if trace_id or session_id:
        kept = []
        for ln in rows:
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if trace_id and rec.get("trace_id") != trace_id:
                continue
            if session_id and rec.get("session_id") != session_id:
                continue
            kept.append(ln)
        rows = kept
    if lines is not None:
        rows = rows[-lines:]
    return Result(context="\n".join(rows) if rows else "(no matching trace records)")


def _dispatch(args: argparse.Namespace) -> Result:
    if args.cmd == "version":
        return Result(context=__version__)
    if args.cmd == "hook":
        text = read_stdin_text()
        try:
            event = json.loads(text) if text.strip() else {}
        except ValueError:
            event = {}
        if not isinstance(event, dict):
            event = {}
        if args.cwd and not event.get("cwd"):
            event["cwd"] = args.cwd
        return _handle(event)
    if args.cmd == "install":
        agents = [a.strip() for a in args.agents.split(",") if a.strip()]
        return install(args.target, args.stack, agents, args.dry_run, args.overwrite, args.ci)
    if args.cmd == "trace":
        root = find_repo_root(os.path.abspath(args.cwd or os.getcwd())) or os.path.abspath(args.cwd or os.getcwd())
        if args.trace_cmd == "tail":
            return trace_show(root, lines=args.lines)
        return trace_show(root, trace_id=args.trace_id, session_id=args.session_id)
    ctx = Ctx(args.cwd)
    t0 = time.monotonic()

    def done(span: str, result: Result, layer: str = "cli", **extra: Any) -> Result:
        ctx.trace(span, result, layer=layer, event=args.cmd, duration_ms=(time.monotonic() - t0) * 1000, **extra)
        return result

    if args.cmd == "guard-command":
        words = [w for w in args.command if w != "--"] if args.command[:1] == ["--"] else args.command
        command = " ".join(words) if len(words) != 1 else words[0]
        result = guard_command(ctx, command)
        return done("guard_command", result, rule_id=_trace_rule_id(result.message),
                    cmd_hash=_trace_hash(command), cmd_len=len(command))
    if args.cmd == "guard-path":
        return done("guard_path", guard_path(ctx, args.path, args.mode),
                    path=_trace_path(args.path), mode=args.mode)
    if args.cmd == "format-file":
        return done("format_file", format_file(ctx, args.path), path=_trace_path(args.path))
    if args.cmd == "run-checks":
        return done("run_checks", run_checks(ctx, args.session, args.continuing, args.force))
    if args.cmd == "session-context":
        return done("session_context", session_context(ctx, args.session))
    if args.cmd == "scan-secrets":
        return done("scan_secrets", scan_secrets(ctx, staged=not args.all))
    if args.cmd == "pre-commit":
        return done("pre_commit", pre_commit(ctx), layer="git-hook")
    if args.cmd == "pre-push":
        return done("pre_push", pre_push(ctx, read_stdin_text()), layer="git-hook")
    if args.cmd == "ci":
        return done("ci", ci(ctx), layer="ci")
    raise ValueError(f"unknown command {args.cmd}")


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdio()
    args = build_parser().parse_args(argv)
    try:
        return emit(_dispatch(args))
    except Exception:
        tb = traceback.format_exc()
        cwd = args.cwd or os.getcwd()
        log(find_repo_root(cwd) or cwd, "internal error:\n" + tb, "ERROR")
        if strict():
            sys.stderr.write(tb)
            return EXIT_INTERNAL
        return 0


if __name__ == "__main__":
    sys.exit(main())
