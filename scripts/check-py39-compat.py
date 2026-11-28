#!/usr/bin/env python
"""Flag syntax and stdlib APIs that do not exist on Python 3.9.

The test matrix runs every Python leg on 3.9 and 3.12 (`.github/workflows/ci-python.yml`),
but development happens on a newer interpreter. A 3.10+ construct therefore imports fine
locally, passes `ruff` (whose `target-version = "py39"` only drives *upgrade* suggestions,
not availability errors) and then fails the 3.9 leg — which is exactly how
`zip(..., strict=True)` reached `main` and broke six tests only in CI.

The guard is deliberately narrow: it reports constructs whose 3.9 behaviour is a hard
`SyntaxError` or `TypeError`, and it says which version introduced each one. Judgement
calls (`from __future__ import annotations` makes `X | Y` annotations safe, so annotations
are never reported) stay out of scope; only runtime-visible uses are flagged.

Exit codes: 0 = clean, 1 = findings, 2 = the guard could not run.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Everywhere first-party Python is written. `scripts/` counts because the scripts CI
#: jobs also run under the 3.9 leg.
TARGETS = (
    REPO / "python" / "ultimate_coders",
    REPO / "tests" / "python",
    REPO / "scripts",
)

#: `from typing import <name>` members added after 3.9.
NEW_TYPING = {
    "TypeAlias": "3.10",
    "ParamSpec": "3.10",
    "Self": "3.11",
    "LiteralString": "3.11",
    "Never": "3.11",
    "NotRequired": "3.11",
    "Required": "3.11",
    "Unpack": "3.11",
    "override": "3.12",
    "TypeAliasType": "3.12",
}

#: `from enum import <name>` members added after 3.9.
NEW_ENUM = {"StrEnum": "3.11", "ReprEnum": "3.11"}

#: `module.attr` references that only exist on newer interpreters.
NEW_ATTRIBUTES = {
    ("itertools", "pairwise"): "3.10",
    ("datetime", "UTC"): "3.11",
    ("asyncio", "timeout"): "3.11",
    ("asyncio", "TaskGroup"): "3.11",
    ("asyncio", "Runner"): "3.11",
    ("contextlib", "chdir"): "3.11",
    ("enum", "StrEnum"): "3.11",
}

#: Handler types that count as "the import was deliberately guarded".
GUARD_HANDLERS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})

#: (relative path, rule) -> why shipping this on 3.9 is nevertheless correct.
EXEMPT: dict[tuple[str, str], str] = {}


def _guarded_nodes(tree: ast.AST) -> set[int]:
    """Ids of nodes inside a `try` body whose handlers catch an import failure."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for handler in node.handlers:
            caught = handler.type
            names: list[str] = []
            if isinstance(caught, ast.Name):
                names = [caught.id]
            elif isinstance(caught, ast.Tuple):
                names = [elt.id for elt in caught.elts if isinstance(elt, ast.Name)]
            if GUARD_HANDLERS.intersection(names):
                catches = True
                break
        if not catches:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                guarded.add(id(child))
    return guarded


def _names(node: ast.AST) -> list[str]:
    """Dotted name for `a.b.c`, else []."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    parts.append(node.id)
    return list(reversed(parts))


def _uses_pep604(subject: ast.AST) -> bool:
    """True when a runtime union expression (`A | B`) is handed to isinstance/issubclass."""
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) for node in ast.walk(subject)
    )


def scan(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """Findings for one file as (line, rule, detail)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        # Under the 3.9 leg this is the normal way a 3.10-only statement (`match`) shows
        # up: the parser refuses before any AST exists.
        return [(exc.lineno or 0, "unparseable", f"does not parse here: {exc.msg}")]
    guarded = _guarded_nodes(tree)
    findings: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        # statements that do not exist before 3.10/3.11
        match_node = getattr(ast, "Match", None)
        if match_node is not None and isinstance(node, match_node):
            findings.append((node.lineno, "match-statement", "`match` requires 3.10"))
        try_star = getattr(ast, "TryStar", None)
        if try_star is not None and isinstance(node, try_star):
            findings.append((node.lineno, "except-star", "`except*` requires 3.11"))

        if isinstance(node, ast.Call):
            func = node.func
            full = _names(func)
            if full and full[-1] == "zip" and any(kw.arg == "strict" for kw in node.keywords):
                findings.append((node.lineno, "zip-strict", "`zip(strict=...)` requires 3.10"))
            if full and full[-1] in {"isinstance", "issubclass"} and len(node.args) >= 2:
                if _uses_pep604(node.args[1]):
                    findings.append(
                        (
                            node.lineno,
                            "pep604-runtime-union",
                            f"`{full[-1]}(..., A | B)` requires 3.10",
                        )
                    )

        # Attribute reads, not just calls: `datetime.UTC` is a constant reference and
        # `itertools.pairwise` is reached through a call — one branch covers both, and a
        # call is reported once because only the inner Attribute is inspected.
        if isinstance(node, ast.Attribute):
            full = _names(node)
            if len(full) > 1:
                version = NEW_ATTRIBUTES.get((full[0], full[-1]))
                if version is not None:
                    findings.append(
                        (
                            node.lineno,
                            f"api:{'.'.join(full)}",
                            f"`{'.'.join(full)}` requires {version}",
                        )
                    )
            if node.attr == "bit_count":
                findings.append(
                    (node.lineno, "api:int.bit_count", "`int.bit_count()` requires 3.10")
                )

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "typing":
                for alias in node.names:
                    version = NEW_TYPING.get(alias.name)
                    if version is not None:
                        findings.append(
                            (
                                node.lineno,
                                f"typing:{alias.name}",
                                f"`typing.{alias.name}` requires {version}",
                            )
                        )
            if module == "enum":
                for alias in node.names:
                    version = NEW_ENUM.get(alias.name)
                    if version is not None:
                        findings.append(
                            (
                                node.lineno,
                                f"enum:{alias.name}",
                                f"`enum.{alias.name}` requires {version}",
                            )
                        )
            if module == "tomllib" and id(node) not in guarded:
                findings.append(
                    (
                        node.lineno,
                        "stdlib:tomllib",
                        "`tomllib` requires 3.11; import it behind `try`/`except ImportError`",
                    )
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tomllib" and id(node) not in guarded:
                    findings.append(
                        (
                            node.lineno,
                            "stdlib:tomllib",
                            "`tomllib` requires 3.11; import it behind `try`/`except ImportError`",
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

    print(f"3.9 compatibility: {scanned} file(s) scanned, {exempted} documented exemption(s)")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"3.9 compatibility check failed: {len(problems)} problem(s)")
        return 1
    print("3.9 compatibility check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
