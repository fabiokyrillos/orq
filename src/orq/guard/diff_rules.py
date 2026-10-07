"""Post-execution guard: deterministic rules over the staged index (SPEC 10.4 and 9.1).

The index is exactly what the iteration commit will contain, so these rules see the real change.
`removed_test`, `removed_export`, `removed_route` and `dependency_removed` are heuristics: a false
positive costs one owner answer, a false negative is caught by the reviewer's standing rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orq.config import GuardConfig
from orq.git.manager import GitManager
from orq.guard.rules import glob_match

_TEST_DEFS = [re.compile(p) for p in (
    r"^\s*(?:async\s+)?def\s+(test_\w+)",
    r"^\s*(?:it|test|describe)\(\s*['\"`]([^'\"`]+)",
    r"^\s*fn\s+(test_\w+)",
    r"^\s*func\s+(Test\w+)",
)]
_EXPORT_DEFS = [re.compile(p) for p in (
    r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(",
    r"^class\s+([A-Za-z]\w*)",
    r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)",
    r"^\s*pub\s+(?:async\s+)?fn\s+(\w+)",
    r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)",
)]
_ROUTE_RE = re.compile(r"^\s*(?:@\w+\.(?:get|post|put|delete|patch|route)\(|\b(?:app|router)\.(?:get|post|put|delete|patch)\()")
_MANIFESTS = re.compile(r"(?:^|/)(?:pyproject\.toml|requirements[^/]*\.txt|package\.json|Cargo\.toml|go\.mod)$")
# A dependency line: `"httpx>=0.27"`, `httpx==1.0`, `"lodash": "^4.17"`, `serde = "1"`, `  golang.org/x/net v0.1`.
_DEP_LINE = re.compile(r"""^\s*["']?([A-Za-z0-9_.@/-]+)["']?\s*(?:[><=~!^\[]|:\s*["'][\^~]?\d|\s+v\d|=\s*["{])""")
_FILE_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_SKIP_PREFIXES = ("---", "+++", "@@", "diff ", "index ", "similarity", "rename", "new file", "deleted file", "old mode", "new mode", "Binary")


@dataclass(frozen=True)
class DiffViolation:
    rule: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}: {self.detail}"


def evaluate_staged(git: GitManager, worktree: Path, *, protected_paths: list[str], guard: GuardConfig,
                    base: str | None = None) -> list[DiffViolation]:
    """With `base` (the run's base commit), a removal only counts when the base has the removed thing: the run may
    rework or drop what it added in an earlier iteration without asking the owner (Phase 6 finding)."""
    violations: list[DiffViolation] = []
    base_text: dict[str, str | None] = {}

    def on_base(path: str) -> str | None:
        if path not in base_text:
            base_text[path] = git.show_file(worktree, base, path) if base else ""
        return base_text[path]

    for status, path in git.staged_name_status(worktree):
        if status == "D" and on_base(path) is not None:
            violations.append(DiffViolation("deleted_file", path, "file deleted"))
        for pattern in protected_paths:
            if glob_match(pattern, path):
                violations.append(DiffViolation("protected_path", path, f"matches protected path {pattern}"))
                break

    removed, added = _split_patch(git.staged_patch(worktree))
    added_text = "\n".join(line for lines in added.values() for line in lines)
    for path, lines in removed.items():
        original = on_base(path)
        if original is None:
            continue  # the file is new in this run: nothing the owner had is lost

        def in_base(text: str) -> bool:
            return not base or text in original  # type: ignore[operator]

        for line in lines:
            name = _first_match(_TEST_DEFS, line)
            if name:
                if name not in added_text and in_base(name):
                    violations.append(DiffViolation("removed_test", path, f"test {name} removed"))
                continue
            name = _first_match(_EXPORT_DEFS, line)
            if name:
                if not name.startswith("_") and not re.search(rf"\b{re.escape(name)}\b", added_text) and in_base(name):
                    violations.append(DiffViolation("removed_export", path, f"{name} removed"))
                continue
            if _ROUTE_RE.search(line):
                if line.strip() not in added_text and in_base(line.strip()):
                    violations.append(DiffViolation("removed_route", path, line.strip()))
                continue
            if _MANIFESTS.search(path):
                dep = _DEP_LINE.match(line)
                if dep and not _dependency_kept(dep.group(1), added.get(path, [])) and in_base(dep.group(1)):
                    violations.append(DiffViolation("dependency_removed", path, f"{dep.group(1)} removed"))

    net = 0
    for plus, minus, path in git.staged_numstat(worktree):
        if any(glob_match(g, path) for g in guard.source_globs):
            net += minus - plus
    if net > guard.max_net_deleted_lines:
        violations.append(DiffViolation("negative_balance", "*",
                                        f"{net} more source lines deleted than added (limit {guard.max_net_deleted_lines})"))
    return violations


def _dependency_kept(name: str, added_lines: list[str]) -> bool:
    for line in added_lines:
        match = _DEP_LINE.match(line)
        if match and match.group(1) == name:
            return True
    return False


def _split_patch(patch: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Removed and added lines per file (path after rename), header lines dropped."""
    removed: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}
    current = ""
    for line in patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            current = header.group(2)
            continue
        if line.startswith(_SKIP_PREFIXES):
            continue
        if line.startswith("-"):
            removed.setdefault(current, []).append(line[1:])
        elif line.startswith("+"):
            added.setdefault(current, []).append(line[1:])
    return removed, added


def _first_match(patterns: list[re.Pattern[str]], line: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None
