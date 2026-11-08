#!/usr/bin/env python3
"""Dry-run readiness check for true replan execute (V8 T18d / D14).

Prints an ops checklist of precursor flags and docs so a human can later
authorize true replan execute. This script:

* NEVER sets any ``UC_REPLAN_*`` / ``UC_COMPLETION_CONDITIONS_ENFORCE`` env var
* NEVER invents or flips a ``UC_REPLAN_EXECUTE_ENFORCE`` (or similar) gate
* NEVER rewrites Compose / image defaults
* NEVER bumps digest, re-decomposes, or calls ``admit_execution``
* exits non-zero when hard blockers are present

Hard blocker (today): readiness runbook file missing from the repo tree.

Truthy reporting matches UC enforce flags (Python ``_TRUTHY_ENV`` / Rust
``enforce_flag_enabled``): strip + ascii-lowercase token in
``{"1", "true", "yes", "on"}``.

Usage: ``python scripts/check-replan-execute-readiness.py``
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNBOOK_REL = "docs/architecture/replan-execute-readiness-runbook.md"

PROPOSAL_ENV = "UC_REPLAN_PROPOSAL_ENFORCE"
CHECKLIST_ENV = "UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE"
ACCOUNTING_ENV = "UC_REPLAN_ACCOUNTING_ENFORCE"
COMPLETION_ENV = "UC_COMPLETION_CONDITIONS_ENFORCE"

PRECURSOR_ENVS: tuple[str, ...] = (
    PROPOSAL_ENV,
    CHECKLIST_ENV,
    ACCOUNTING_ENV,
    COMPLETION_ENV,
)

TRUTHY_TOKENS = frozenset({"1", "true", "yes", "on"})


def enforce_flag_enabled(raw: str | None) -> bool:
    """Match UC enforce-flag truthy tokens (trim + case-insensitive).

    Same contract as Python ``_TRUTHY_ENV`` helpers and Rust
    ``enforce_flag_enabled`` in ``proactive_service.rs``.
    """
    if raw is None:
        return False
    return raw.strip().lower() in TRUTHY_TOKENS


def _flag_line(name: str, raw: str | None) -> str:
    on = enforce_flag_enabled(raw)
    state = "ON" if on else "off"
    if raw is None:
        suffix = " (unset)"
    else:
        suffix = f" (raw={raw!r})"
    return f"  {name}: {state}{suffix}"


def hard_blockers(
    env: Mapping[str, str | None],
    *,
    runbook_rel: str = RUNBOOK_REL,
    repo: Path = REPO,
) -> list[str]:
    """Return human-readable hard blockers for the given snapshot.

    ``env`` is accepted for API symmetry with sibling readiness scripts; flag
    state is advisory only and never a hard blocker here (execute stays
    deferred — ops must authorize later).
    """
    del env  # precursor flags are reported, not blocking
    blockers: list[str] = []
    runbook = repo / runbook_rel
    if not runbook.is_file():
        blockers.append(
            f"runbook missing: {runbook_rel} "
            "(ops checklist required before any future execute authorization)"
        )
    return blockers


def assemble_checklist(
    env: Mapping[str, str | None],
    *,
    runbook_rel: str = RUNBOOK_REL,
    repo: Path = REPO,
) -> list[str]:
    """Build the printed checklist lines (pure; path joins only)."""
    runbook = repo / runbook_rel
    lines = [
        "Replan execute readiness (dry-run; no env/Compose mutation; "
        "execute remains deferred)",
        _flag_line(PROPOSAL_ENV, env.get(PROPOSAL_ENV)),
        _flag_line(CHECKLIST_ENV, env.get(CHECKLIST_ENV)),
        _flag_line(ACCOUNTING_ENV, env.get(ACCOUNTING_ENV)),
        _flag_line(COMPLETION_ENV, env.get(COMPLETION_ENV)),
        f"  runbook: {runbook_rel}"
        + (" (present)" if runbook.is_file() else " (MISSING)"),
        "  note: no UC_REPLAN_EXECUTE_ENFORCE (or similar) gate exists; "
        "this probe does not enable execute",
        "  next: see durable-runtime-upgrade-notes.md for gateway/worker "
        "contract lockstep; trust-eval ticket 18 still owns true execute",
    ]
    return lines


def _env_snapshot(
    env: Mapping[str, str | None] | None,
) -> Mapping[str, str | None]:
    if env is None:
        # Read-only view of process env — never assign back into os.environ.
        return {name: os.environ.get(name) for name in PRECURSOR_ENVS}
    return {name: env.get(name) for name in PRECURSOR_ENVS}


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str | None] | None = None,
) -> int:
    del argv  # reserved for future flags; dry-run takes none today
    snapshot = _env_snapshot(env)
    # Resolve REPO at call time so tests can monkeypatch the module global.
    repo = REPO

    for line in assemble_checklist(snapshot, repo=repo):
        print(line)

    blockers = hard_blockers(snapshot, repo=repo)
    if blockers:
        print("HARD BLOCKERS:")
        for b in blockers:
            print(f"  - {b}")
        print("replan execute readiness check failed.")
        return 1

    print("replan execute readiness check passed (no hard blockers).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
