# 49 — Async task hygiene guard (T49)

**What to build:** A guard that walks `python/ultimate_coders/**` and flags a
`create_task(...)` whose task is never kept, awaited, gathered, returned or cancelled in
the same function — the shape that can be garbage-collected mid-flight and whose failure
surfaces only as "Task exception was never retrieved". Plus its tests, a Scripts CI job,
and the README CI table row.

**Why a guard rather than a fix:** T48 fixed the one instance found by reading the code.
The class is mechanically decidable from the AST — a task must stay reachable — so the
next instance should fail CI instead of relying on the next audit remembering to look.

**Status:** implemented (`scripts/check-async-task-hygiene.py`,
`tests/python/test_check_async_task_hygiene.py`, `.github/workflows/ci-scripts.yml`,
`README.md`)

- [x] Accepts: stored on `self`, added to a collection, given a done callback, awaited,
      shielded/gathered, returned to the caller, cancelled in scope
- [x] Reports: assigned-but-unused `create_task` (both `asyncio.` and `loop.` forms)
- [x] Negative controls in the test prove it can say no; the real repo is clean
- [x] Runs in Scripts CI (3.9 + 3.12) next to the other guards

**Boundary:** syntactic by design (it cannot prove a task finishes, only that a reference
exists). Exception *observation* — the other half of T48 — is not covered here; the
concrete fixes for that stay in the code that owns them.
