"""Deterministic risk rules. They run before the model and can only raise risk."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .diffparse import FileDiff

LEVELS = ["low", "medium", "high"]


@dataclass
class RiskFinding:
    level: str
    rule: str
    path: str
    line: int | None
    message: str


PATH_RULES: list[tuple[str, str, str]] = [
    (r"^\.github/workflows/", "high", "CI workflow changed: can alter what runs with repo secrets"),
    (r"(^|/)(auth|security|permissions?|crypto)(/|\.|_)", "high", "security-sensitive module"),
    (r"(^|/)migrations?/", "high", "database migration"),
    (r"(^|/)(Dockerfile|docker-compose\.ya?ml)$", "medium", "container/runtime config"),
    (r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|package(-lock)?\.json|poetry\.lock|uv\.lock)$",
     "medium", "dependency change"),
    (r"(^|/)\.env", "high", "environment file committed"),
]

LINE_RULES: list[tuple[str, str, str]] = [
    (r"(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_\-]{30,}"
     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)", "high", "possible secret committed"),
    (r"\b(eval|exec)\s*\(", "medium", "dynamic code execution"),
    (r"subprocess\.[a-z_]+\(.*shell\s*=\s*True", "high", "shell=True subprocess"),
    (r"\bpickle\.loads?\(", "medium", "unpickling untrusted data"),
    (r"verify\s*=\s*False", "high", "TLS verification disabled"),
    (r"\b(TODO|FIXME|XXX)\b", "low", "unresolved TODO/FIXME"),
    (r"(password|secret|token)\s*=\s*['\"][^'\"]{4,}['\"]", "high", "hardcoded credential"),
    (r"\bprint\(", "low", "stray print() call"),
]

_PATHS = [(re.compile(p), lvl, msg) for p, lvl, msg in PATH_RULES]
_LINES = [(re.compile(p, re.IGNORECASE if "password" in p else 0), lvl, msg)
          for p, lvl, msg in LINE_RULES]


def assess(files: list[FileDiff]) -> list[RiskFinding]:
    out: list[RiskFinding] = []
    for f in files:
        for rx, lvl, msg in _PATHS:
            if rx.search(f.path):
                out.append(RiskFinding(lvl, "path", f.path, None, msg))
        if f.status == "deleted" and re.search(r"(^|/)tests?/|test_[^/]*\.py$", f.path):
            out.append(RiskFinding("high", "deleted_test", f.path, None, "test file deleted"))
        if f.path.startswith("tests/") or "/test_" in f.path or f.path.startswith("test_"):
            removed_asserts = sum(1 for r in f.removed if r.strip().startswith("assert"))
            added_asserts = sum(1 for a in f.added.values() if a.strip().startswith("assert"))
            if removed_asserts > added_asserts:
                out.append(RiskFinding("medium", "weakened_tests", f.path, None,
                                       f"{removed_asserts - added_asserts} assertion(s) removed"))
        if f.status == "binary":
            out.append(RiskFinding("medium", "binary", f.path, None, "binary file changed"))
        for n, text in f.added.items():
            for rx, lvl, msg in _LINES:
                if rx.search(text):
                    out.append(RiskFinding(lvl, "line", f.path, n, msg))
    total = sum(f.additions + f.deletions for f in files)
    if total > 800:
        out.append(RiskFinding("medium", "size", "*", None, f"large change ({total} lines)"))
    return out


def max_level(*levels: str) -> str:
    known = [lv for lv in levels if lv in LEVELS]
    return max(known, key=LEVELS.index) if known else "low"
