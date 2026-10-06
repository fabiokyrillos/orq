"""Pure classification of a Claude Code tool call against the guard rules (SPEC 10.3).

No I/O here: the hook, the runner and the tests all call `classify`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_ENV_PREFIX = re.compile(r"^\w+=")
# Bare `truncate <name>` is left out on purpose: it collides with grep/log text far more often than it appears in SQL.
_SQL_RE = re.compile(r"\b(?:drop\s+(?:table|database|schema|index|view)|truncate\s+table)\s+\S", re.IGNORECASE)
_PS_RECURSE_RE = re.compile(r"\b(?:remove-item|ri|rm|del|erase|rd|rmdir)\b[^|;&]*\s-recurse\b", re.IGNORECASE)
_CMD_RECURSE_RE = re.compile(r"\b(?:rmdir|rd|del|erase)\b[^|;&]*\s/s\b", re.IGNORECASE)
_ABS_PATH = r"((?:[A-Za-z]:[\\/]|/)[^\s\"'|;&]+)"
_REDIRECT_RE = re.compile(r">{1,2}\s*\"?" + _ABS_PATH)
_TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?\"?" + _ABS_PATH)
_GITBASH_DRIVE = re.compile(r"^/([A-Za-z])/(.*)$")
_DEP_REMOVAL = {
    ("uv", "remove"), ("pip", "uninstall"), ("pip3", "uninstall"), ("poetry", "remove"), ("cargo", "remove"),
    ("npm", "uninstall"), ("npm", "remove"), ("npm", "rm"), ("npm", "un"), ("npm", "r"),
    ("yarn", "remove"), ("pnpm", "remove"), ("pnpm", "rm"),
}


@dataclass(frozen=True)
class Violation:
    rule: str
    description: str


def action_key(tool_name: str, tool_input: dict) -> str:
    """Stable key for one exact action; the allow token file is named after it."""
    payload = tool_name + "\n" + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def describe_action(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return f"Bash: {str(tool_input.get('command', '')).strip()}"
    key = FILE_TOOLS.get(tool_name)
    if key:
        return f"{tool_name}: {tool_input.get(key, '')}"
    return f"{tool_name}: {json.dumps(tool_input, ensure_ascii=False)[:200]}"


def classify(tool_name: str, tool_input: dict, *, worktree: Path | None, protected_paths: list[str]) -> Violation | None:
    if tool_name == "Bash":
        return _classify_bash(str(tool_input.get("command", "")), worktree)
    key = FILE_TOOLS.get(tool_name)
    if key and tool_input.get(key):
        return _classify_path(str(tool_input[key]), worktree, protected_paths)
    return None


# Bash


def _classify_bash(command: str, worktree: Path | None) -> Violation | None:
    if not command.strip():
        return None
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = [t for t in _tokens(segment) if not _ENV_PREFIX.match(t)]
        if tokens and tokens[0].lower() == "sudo":
            tokens = tokens[1:]
        if not tokens:
            continue
        head = tokens[0].lower()
        args = tokens[1:]
        if head == "git" and args:
            hit = _classify_git(args)
            if hit:
                return hit
        if head == "rm" and any(_is_short_flag(a) and ("r" in a or "R" in a) or a == "--recursive" for a in args):
            return Violation("recursive_delete", "recursive delete")
        if head == "python" and len(args) >= 3 and args[0] == "-m" and args[1] == "pip" and args[2] == "uninstall":
            return Violation("dependency_removal", "dependency removal")
        if args and (head, args[0].lower()) in _DEP_REMOVAL:
            return Violation("dependency_removal", "dependency removal")
    if _PS_RECURSE_RE.search(command) or _CMD_RECURSE_RE.search(command):
        return Violation("recursive_delete", "recursive delete")
    if _SQL_RE.search(command):
        return Violation("sql_destructive", "destructive SQL")
    if worktree is not None:
        for match in (*_REDIRECT_RE.finditer(command), *_TEE_RE.finditer(command)):
            if not _inside(_normalize(match.group(1)), worktree):
                return Violation("write_outside_worktree", f"write to {match.group(1)} outside the worktree")
    return None


def _classify_git(args: list[str]) -> Violation | None:
    sub = args[0].lower()
    rest = args[1:]
    flags = {a.lower() for a in rest}
    if sub == "push":
        if flags & {"--force", "-f", "--force-with-lease"}:
            return Violation("git_force_push", "git force push")
        if flags & {"--delete", "-d"} or any(a.startswith(":") and len(a) > 1 for a in rest):
            return Violation("git_branch_delete", "remote branch deletion")
    if sub == "reset" and "--hard" in flags:
        return Violation("git_reset_hard", "git reset --hard")
    if sub == "branch" and ({a for a in rest} & {"-d", "-D", "--delete"}):
        return Violation("git_branch_delete", "branch deletion")
    if sub == "clean" and any(_is_short_flag(a) and ("f" in a or "x" in a) for a in rest):
        return Violation("git_clean", "git clean")
    return None


def _is_short_flag(token: str) -> bool:
    return token.startswith("-") and not token.startswith("--") and len(token) > 1


def _tokens(segment: str) -> list[str]:
    try:
        return [t.strip('"') for t in shlex.split(segment, posix=False)]
    except ValueError:
        return segment.split()


# paths


def _classify_path(raw: str, worktree: Path | None, protected_paths: list[str]) -> Violation | None:
    path = _normalize(raw)
    if path.is_absolute():
        if worktree is not None and not _inside(path, worktree):
            return Violation("write_outside_worktree", f"{raw} is outside the worktree")
        relative = _relative_to(path, worktree) if worktree is not None else path.as_posix()
    else:
        relative = path.as_posix()
    for pattern in protected_paths:
        if glob_match(pattern, relative):
            return Violation("protected_path", f"{relative} matches protected path {pattern}")
    return None


def _normalize(raw: str) -> Path:
    text = raw.strip().strip('"').strip("'")
    match = _GITBASH_DRIVE.match(text)  # Git Bash style /c/dev/x
    if match:
        text = f"{match.group(1).upper()}:/{match.group(2)}"
    path = Path(text)
    return path.resolve() if path.is_absolute() else path


def _inside(path: Path, root: Path) -> bool:
    try:
        _relative_to(path, root)
        return True
    except ValueError:
        return False


def _relative_to(path: Path, root: Path) -> str:
    # PureWindowsPath compares case-insensitively; on POSIX a plain Path would do.
    return PureWindowsPath(str(path.resolve())).relative_to(PureWindowsPath(str(root.resolve()))).as_posix()


def glob_match(pattern: str, relative_posix: str) -> bool:
    """fnmatch with `**` crossing directories. `.github/**` matches `.github/workflows/ci.yml`."""
    regex = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        elif ch == "*":
            regex += "[^/]*"
            i += 1
        elif ch == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(ch)
            i += 1
    return re.fullmatch(regex, relative_posix) is not None
