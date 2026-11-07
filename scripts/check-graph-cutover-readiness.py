#!/usr/bin/env python3
"""Dry-run readiness check for graph-primary cutover (V8 P4 / D6 / D10).

Prints an ops checklist for enabling ``UC_GRAPH_SHADOW`` under human
authorization. This script:

* NEVER sets ``UC_GRAPH_SHADOW`` or any other environment variable
* NEVER rewrites Compose / image defaults
* exits non-zero when hard blockers are present

Hard blocker (today): shadow gate is on (exact ``on``, matching gateway
``resolve_graph_shadow_choice``) while ``UC_DATABASE_URL`` is unset/empty.

Usage: ``python scripts/check-graph-cutover-readiness.py``
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNBOOK_REL = "docs/architecture/graph-cutover-runbook.md"
SHADOW_ENV = "UC_GRAPH_SHADOW"
DATABASE_ENV = "UC_DATABASE_URL"


def resolve_graph_shadow_choice(shadow: str | None) -> bool:
    """Match ``resolve_graph_shadow_choice`` in ``crates/uc-grpc-server/src/main.rs``.

    Exact token ``\"on\"`` only — unset, empty, ``1``, ``true``, ``On``, ``OFF``
    all stay off.
    """
    return shadow == "on"


def database_url_is_set(value: str | None) -> bool:
    return bool(value and value.strip())


def hard_blockers(env: Mapping[str, str | None]) -> list[str]:
    """Return human-readable hard blockers for the given env snapshot."""
    shadow_raw = env.get(SHADOW_ENV)
    blockers: list[str] = []
    if resolve_graph_shadow_choice(shadow_raw) and not database_url_is_set(
        env.get(DATABASE_ENV)
    ):
        blockers.append(
            f"{SHADOW_ENV}=on but {DATABASE_ENV} is unset/empty "
            "(gateway cannot wire graph shadow without a database URL)"
        )
    return blockers


def assemble_checklist(
    env: Mapping[str, str | None],
    *,
    runbook_rel: str = RUNBOOK_REL,
    repo: Path = REPO,
) -> list[str]:
    """Build the printed checklist lines (pure; no I/O beyond path joins)."""
    shadow_raw = env.get(SHADOW_ENV)
    shadow_on = resolve_graph_shadow_choice(shadow_raw)
    db_set = database_url_is_set(env.get(DATABASE_ENV))
    runbook = repo / runbook_rel
    lines = [
        "Graph cutover readiness (dry-run; no env/Compose mutation)",
        f"  {SHADOW_ENV}: {'ON (exact on)' if shadow_on else 'off'}"
        + (f" (raw={shadow_raw!r})" if shadow_raw is not None else " (unset)"),
        f"  {DATABASE_ENV}: {'set' if db_set else 'unset/empty'}",
        f"  runbook: {runbook_rel}"
        + (" (present)" if runbook.is_file() else " (MISSING)"),
        "  defaults: Compose/image UC_GRAPH_SHADOW stay off; never infer "
        "graph-only from database URL alone",
        "  next: see durable-runtime-upgrade-notes.md for gateway/worker "
        "lockstep restart before enabling",
    ]
    return lines


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    del argv  # reserved for future flags; dry-run takes none today
    snapshot: Mapping[str, str | None]
    if env is None:
        # Read-only view of process env — never assign back into os.environ.
        snapshot = {SHADOW_ENV: os.environ.get(SHADOW_ENV), DATABASE_ENV: os.environ.get(DATABASE_ENV)}
    else:
        snapshot = {
            SHADOW_ENV: env.get(SHADOW_ENV),
            DATABASE_ENV: env.get(DATABASE_ENV),
        }

    for line in assemble_checklist(snapshot):
        print(line)

    blockers = hard_blockers(snapshot)
    if blockers:
        print("HARD BLOCKERS:")
        for b in blockers:
            print(f"  - {b}")
        print("graph cutover readiness check failed.")
        return 1

    print("graph cutover readiness check passed (no hard blockers).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
