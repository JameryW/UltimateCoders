#!/usr/bin/env python
"""Keep temporary directories in tests owned by pytest.

A full-suite run once left two `tmp*` directories in the repository root. They came from
`tempfile.mkdtemp()` plus a hand-written `finally:` cleanup: that teardown only runs when the
test reaches its own end, so an assertion failure, a `KeyboardInterrupt`, or a killed CI job
leaves the directory behind. `git add -A` then staged those leftovers as embedded repositories
and the line-ending guard's gitlink count jumped from 1 to 4, silently changing a pinned number.

They landed *in the checkout* because `tempfile.gettempdir()` falls back to the current directory
when every configured candidate is refused: on a locked-down runner the process sees
`TEMP=C:\\...\\Temp` but cannot write there, so `mkdtemp()` creates its directory beside the
sources. `.gitignore` covers pytest's own fallback root (`pytest-of-*/`), not an ad-hoc prefix.

`tmp_path` (pytest-owned) and `tempfile.TemporaryDirectory()` (context-managed) both clean up on
every exit path, including failures. Production code is out of scope: per-run HOME directories
under `python/ultimate_coders/agent/sandbox.py` are created by `mkdtemp()` on purpose, because
their lifetime must outlive the call that creates them.

Exit codes: 0 = clean, 1 = findings, 2 = the guard could not run.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Tests are the only place where a leaked directory outlives its run.
TARGETS = (REPO / "tests" / "python",)

#: Name the call is bound to when `tempfile` is imported, per module.
MODULE_ALIAS = "tempfile"
FUNCTION = "mkdtemp"

#: (relative path, rule) -> why the manual directory is nevertheless safe here.
EXEMPT: dict[tuple[str, str], str] = {}


def _imported_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (module aliases for `tempfile`, local names bound to `mkdtemp`)."""
    modules: set[str] = {MODULE_ALIAS}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == MODULE_ALIAS:
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == MODULE_ALIAS:
            for alias in node.names:
                if alias.name == FUNCTION:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def scan(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Return `(line, rule, detail)` for every manually created temp directory."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Unparseable test files are another guard's problem.
        return []

    modules, functions = _imported_names(tree)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == FUNCTION:
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id in modules:
                findings.append(
                    (
                        node.lineno,
                        "tests:mkdtemp",
                        "`tempfile.mkdtemp()` is not cleaned up when the test fails; "
                        "use the `tmp_path` fixture or `tempfile.TemporaryDirectory()`",
                    )
                )
        elif isinstance(func, ast.Name) and func.id in functions:
            findings.append(
                (
                    node.lineno,
                    "tests:mkdtemp",
                    "`mkdtemp()` is not cleaned up when the test fails; "
                    "use the `tmp_path` fixture or `tempfile.TemporaryDirectory()`",
                )
            )
    return sorted(findings)


def main() -> int:
    missing = [target for target in TARGETS if not target.is_dir()]
    if missing:
        for target in missing:
            print(f"FAIL: {target} is not a directory")
        return 2

    problems: list[str] = []
    scanned = 0
    exempted = 0
    for target in TARGETS:
        for path in sorted(target.rglob("*.py")):
            scanned += 1
            relative = path.relative_to(REPO).as_posix()
            for line, rule, detail in scan(path):
                if (relative, rule) in EXEMPT:
                    exempted += 1
                    continue
                problems.append(f"{relative}:{line}: {detail}")

    print(f"test temp dirs: {scanned} file(s) scanned, {exempted} documented exemption(s)")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"test temp dir check failed: {len(problems)} problem(s)")
        return 1
    print("test temp dir check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
