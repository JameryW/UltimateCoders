#!/usr/bin/env python
"""Require a bounded `timeout=` on every `subprocess` call that can block a worker.

`subprocess.run/call/check_output/Popen` without `timeout=` waits forever. On a worker
that means one hung `git` (index.lock, credential prompt, network fetch) or `nvidia-smi`
(driver wedge) silently occupies a slot with nothing to observe or cancel — the process is
still "working" from every scheduler's point of view.

The guard walks `python/ultimate_coders/**` and reports each such call, with a small,
documented exemption list for the two sites that are themselves **child runners** (they
execute the workload; their parent bounds them, and a timeout here would cut a legitimate
long run).

Exit codes: 0 = clean, 1 = findings, 2 = the guard could not run.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TARGET = REPO / "python" / "ultimate_coders"

CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

#: (relative path, enclosing function or "<module>") -> why no timeout is correct.
EXEMPT: dict[tuple[str, str], str] = {
    ("python/ultimate_coders/inference/process.py", "<module>"): (
        "child runner: this process *is* the workload; spawn_command's parent side "
        "enforces the timeout"
    ),
    ("python/ultimate_coders/metainfer_service/execution.py", "main"): (
        "child runner: executes the pinned upstream workload; the supervisor bounds it"
    ),
}


def _call_name(call: ast.Call) -> tuple[str, str] | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in CALLS:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
        return None
    return ("subprocess", func.attr)


def _owners(tree: ast.AST) -> dict[ast.AST, str]:
    """Map each node to the name of its innermost enclosing function, or "<module>"."""
    result: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                result.setdefault(child, node.name)
    return result


def scan(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Findings for one file as (line, function, call name)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [(exc.lineno or 0, "<module>", f"could not parse: {exc.msg}")]
    owners = _owners(tree)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        named = _call_name(node)
        if named is None:
            continue
        if any(keyword.arg == "timeout" for keyword in node.keywords):
            continue
        findings.append((node.lineno, owners.get(node, "<module>"), named[1]))
    return findings


def main() -> int:
    if not TARGET.is_dir():
        print(f"FAIL: {TARGET} is not a directory")
        return 2
    problems: list[str] = []
    scanned = 0
    exempted = 0
    for path in sorted(TARGET.rglob("*.py")):
        scanned += 1
        relative = path.relative_to(REPO).as_posix()
        for line, function, call in scan(path):
            if (relative, function) in EXEMPT:
                exempted += 1
                continue
            problems.append(
                f"{relative}:{line}: subprocess.{call} in {function}() has no timeout="
            )

    print(f"subprocess timeouts: {scanned} module(s) scanned, {exempted} documented exemption(s)")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"subprocess timeout check failed: {len(problems)} problem(s)")
        return 1
    print("subprocess timeout check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
