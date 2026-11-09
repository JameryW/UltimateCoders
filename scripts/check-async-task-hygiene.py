#!/usr/bin/env python
"""Flag `asyncio.create_task` calls whose task reference can be lost.

CPython holds tasks **weakly**: an unreferenced task may be garbage-collected before it
finishes, and its exception is then reported only as "Task exception was never retrieved"
at collection time -- so a background job can vanish with no log line and nothing can
observe it. The 2026-11 audit (T48) found exactly that shape in the dashboard's NATS
connect-retry loop: the task was stored, but its *failure* was not observed, and the
distinction cost a silent event-delivery outage.

This guard decides the part that is decidable by reading the code: a `create_task(...)`
call must keep its task reachable within the same function. The accepted shapes are:

* assigned to an attribute (`self._bg_tasks = ...`, `self._task = ...`);
* stored in a collection or given a done callback (`x.add(...)`, `x.add_done_callback(...)`);
* awaited directly, shielded, or gathered (`await x`, `await asyncio.shield(x)`, ...);
* returned to the caller (who then owns it);
* cancelled in the same function (a task that is explicitly torn down before it can be
  forgotten).

Anything else is reported. The guard is deliberately syntactic: it cannot prove the task
finishes, only that a reference exists -- which is the class of bug it can decide.

Exit codes: 0 = clean, 1 = at least one finding, 2 = the guard could not run.
"""

from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TARGET = REPO / "python" / "ultimate_coders"

#: Calls whose argument is an awaited-anyway handle.
AWAIT_HELPERS = frozenset({"shield", "gather", "wait", "wait_for", "wait_for_first"})

#: Methods that prove the caller kept the handle.
KEEPING_METHODS = frozenset({"add", "add_done_callback", "append", "extend", "discard"})

#: Documented exceptions: (relative path, line, reason). Keep this list short and
#: justified -- an entry here is a claim that the finding is not a lost task.
EXEMPT: frozenset[tuple[str, int]] = frozenset()


def _is_create_task(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task"
    )


def _enclosing_assignment(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """Walk up through wrappers (IfExp, Await) to the statement that binds the task."""
    current = node
    for _ in range(6):
        parent = parents.get(current)
        if parent is None:
            return None
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.Return, ast.Expr)):
            return parent if isinstance(parent, (ast.Assign, ast.AnnAssign)) else None
        if isinstance(parent, ast.IfExp):
            grandparent = parents.get(parent)
            if isinstance(grandparent, (ast.Assign, ast.AnnAssign)):
                return grandparent
        current = parent
    return None


def _target_name(assignment: ast.Assign | ast.AnnAssign) -> str | None:
    target = assignment.targets[0] if isinstance(assignment, ast.Assign) else assignment.target
    if isinstance(target, ast.Attribute):
        return ast.unparse(target)
    if isinstance(target, ast.Name):
        return target.id
    return None


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _carries_name(call: ast.Call, name: str) -> bool:
    arguments = list(call.args) + [keyword.value for keyword in call.keywords]
    return any(isinstance(argument, ast.Name) and argument.id == name for argument in arguments)


def _uses_in_function(body: list[ast.stmt], name: str) -> bool:
    """True when the bound name is kept, awaited, gathered or cancelled in scope."""
    short = name.rsplit(".", 1)[-1]
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                helper = _call_name(node)
                if helper in AWAIT_HELPERS and _carries_name(node, short):
                    return True
                if helper in KEEPING_METHODS | {"cancel"}:
                    if _carries_name(node, short):
                        return True
                    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
                    if isinstance(receiver, ast.Name) and receiver.id == short:
                        return True
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Name):
                if node.value.id == short:
                    return True
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
                if node.value.id == short:
                    return True
    return False


def scan(path: pathlib.Path) -> list[tuple[int, str]]:
    """Findings for one file, as (line, reason)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # a syntax error is the test suite's problem, not this guard
        return [(exc.lineno or 0, f"could not parse: {exc.msg}")]
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not _is_create_task(node):
            continue
        assignment = _enclosing_assignment(node, parents)
        if assignment is None:
            # Used inline: `return asyncio.create_task(...)` or passed to an await
            # helper -- the caller/awaiter owns the reference.
            continue
        name = _target_name(assignment)
        if name is None:
            findings.append((node.lineno, "create_task bound to something unreadable"))
            continue
        if name.startswith("self."):
            continue
        # The enclosing function body: bindings must be observable in it.
        owner: ast.AST | None = parents.get(assignment)
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
        ):
            owner = parents.get(owner)
        body = owner.body if owner is not None else []
        if not _uses_in_function(body, name):
            findings.append(
                (node.lineno, f"{name} is never kept, awaited, gathered or cancelled")
            )
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
        for line, reason in scan(path):
            if (relative, line) in EXEMPT:
                continue
            problems.append(f"{relative}:{line}: {reason}")

    print(f"async task hygiene: {scanned} module(s) scanned")
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"async task hygiene check failed: {len(problems)} problem(s)")
        return 1
    print("async task hygiene check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
