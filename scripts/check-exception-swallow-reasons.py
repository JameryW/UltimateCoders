#!/usr/bin/env python
"""Require a written reason for a broad, silently-swallowed exception.

`except Exception: pass` erases the only evidence that something went wrong. Most of
these handlers in this repository are legitimate best-effort paths (metrics publishing,
optional MCP features, cleanup during an error path) -- but a reader cannot tell a
deliberate swallow from a bug, and neither can a reviewer skimming a diff.

So the rule is not "never swallow"; it is "swallow out loud": a bare `pass` handler that
catches `Exception`/`BaseException`/everything must carry a `# best-effort: <reason>`
comment, in the handler body or on the lines immediately above it. Narrow catches
(`FileNotFoundError`, `TimeoutError`, ...) are out of scope: the exception type is the
explanation there.

Exit codes: 0 = clean, 1 = at least one unexplained swallow, 2 = the guard could not run.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TARGET = REPO / "python" / "ultimate_coders"

BROAD = frozenset({"Exception", "BaseException", ""})
MARKER = "# best-effort:"
#: How many lines above the handler may carry the marker.
LOOKBACK = 4


def scan(path: pathlib.Path) -> list[tuple[int, str]]:
    """Findings for one file as (line, caught type)."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [(0, f"unreadable: {exc}")]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"could not parse: {exc.msg}")]
    lines = source.splitlines()
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = "" if node.type is None else ast.unparse(node.type).strip()
        if caught not in BROAD:
            continue
        if not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
            continue
        start = max(0, node.lineno - 1 - LOOKBACK)
        end = min(len(lines), node.body[0].end_lineno or node.lineno)
        window = "\n".join(lines[start:end])
        if MARKER in window:
            continue
        findings.append((node.lineno, caught or "bare except"))
    return findings


def main() -> int:
    if not TARGET.is_dir():
        print(f"FAIL: {TARGET} is not a directory")
        return 2
    problems: list[str] = []
    scanned = 0
    for path in sorted(TARGET.rglob("*.py")):
        scanned += 1
        relative = path.relative_to(REPO).as_posix()
        for line, caught in scan(path):
            problems.append(f"{relative}:{line}: `except {caught}: pass` has no '{MARKER}' reason")

    print(f"exception swallow reasons: {scanned} module(s) scanned")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"exception swallow check failed: {len(problems)} problem(s)")
        return 1
    print("exception swallow check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
