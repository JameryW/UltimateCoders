"""Read face over the knowledge layer: counts only, never captured content.

The knowledge layer captures evidence, claims, attempt rounds and pages into the
authoritative ``RuntimeState``; nothing aggregated them. That left two honest
gaps: the shape of a knowledge base was invisible, and gate *denials* were
returned to the caller and then dropped, so nobody could answer "why is this
knowledge not shared?". This module is the read-only answer:

* attempt rounds by outcome,
* evidence by kind / visibility / verified flag,
* claims by status / visibility,
* gate decisions: allowed vs denied, with a reason histogram,
* knowledge pages by lifecycle (the P4 projection's read face).

Everything is bounded (``limit`` rows per namespace) and log-safe: the output
contains counts and reason codes only - never claim statements, page bodies,
diffs or prompt text. Reading never raises on malformed rows; unknown values are
bucketed as ``"unknown"``. It is a pure function over :class:`RuntimeState`, so
it is testable offline and reusable by the dashboard API or a CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ultimate_coders.knowledge.attempt_chain import NAMESPACE as ATTEMPT_CHAIN_NAMESPACE
from ultimate_coders.knowledge.evidence import CLAIM_NAMESPACE
from ultimate_coders.knowledge.evidence import NAMESPACE as EVIDENCE_NAMESPACE
from ultimate_coders.knowledge.model import safe_text
from ultimate_coders.knowledge.pages import NAMESPACE as PAGES_NAMESPACE
from ultimate_coders.knowledge.rollout import describe_stage, stage_from_env
from ultimate_coders.runtime_state import RuntimeState

SCHEMA = "knowledge-overview/1"
MAX_RECORDS = 5000
UNKNOWN = "unknown"


def _as_rows(state: RuntimeState, namespace: str) -> list[dict[str, Any]]:
    """Read one namespace tolerantly; an unreadable namespace reads as empty."""

    try:
        rows = state.records(namespace)
    except Exception:
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _attempt_of(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The attempt identity mapping carried by a record (``{}`` when absent)."""

    attempt = row.get("attempt")
    return attempt if isinstance(attempt, Mapping) else {}


def _attempt_task(row: Mapping[str, Any]) -> str:
    return _text(_attempt_of(row).get("task_id"))


def _scope_of(row: Mapping[str, Any]) -> Mapping[str, Any]:
    scope = row.get("scope")
    return scope if isinstance(scope, Mapping) else {}


def _page_task(row: Mapping[str, Any]) -> str:
    return _text(_scope_of(row).get("graph_id"))


def _text(value: Any) -> str:
    return safe_text(value, limit=120).strip()


def _word(value: Any) -> str:
    """Normalize an enum-ish value to its lowercase scalar (or ``unknown``)."""

    raw = getattr(value, "value", value)
    word = _text(raw).lower()
    return word or UNKNOWN


def _payload_of(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _matches_task(row: Mapping[str, Any], task_id: str, *, page: bool = False) -> bool:
    """Whether a row belongs to ``task_id`` (no filter when empty)."""

    if not task_id:
        return True
    if page:
        return _page_task(row) == task_id
    return _attempt_task(row) == task_id


def _bounded(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Slice rows to ``limit`` and say whether anything was left out."""

    if len(rows) <= limit:
        return rows, False
    return rows[:limit], True


def _counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _attempt_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(_word(row.get("outcome")) for row in rows)
    return {
        "total": len(rows),
        "by_outcome": dict(sorted(outcomes.items())),
        "completed": outcomes.get("completed", 0),
        "failed": outcomes.get("failed", 0),
        "cleanup_pending": outcomes.get("cleanup_pending", 0),
    }


def _evidence_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "by_kind": _counts(_word(row.get("kind")) for row in rows),
        "by_visibility": _counts(_word(row.get("visibility")) for row in rows),
        "verified": sum(1 for row in rows if bool(row.get("verified"))),
    }


def _claim_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = _counts(_word(row.get("status")) for row in rows)
    return {
        "total": len(rows),
        "by_status": statuses,
        "by_visibility": _counts(_word(row.get("visibility")) for row in rows),
        "promoted": statuses.get("promoted", 0),
    }


def _gate_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate VALIDATION evidence into allowed/denied plus a reason histogram.

    Only rows written by ``claims.record_gate_decision`` carry ``allowed``; any
    other validation evidence is counted under ``other`` so a mixed namespace is
    still visible instead of silently misread.
    """

    allowed = 0
    denied = 0
    other = 0
    reasons: Counter[str] = Counter()
    for row in rows:
        payload = _payload_of(row)
        decision = payload.get("allowed")
        if not isinstance(decision, bool):
            other += 1
            continue
        if decision:
            allowed += 1
            continue
        denied += 1
        for reason in payload.get("reasons") or ():
            word = _word(reason)
            if word != UNKNOWN:
                reasons[word] += 1
    result: dict[str, Any] = {
        "allowed": allowed,
        "denied": denied,
        "other": other,
        "reasons": dict(sorted(reasons.items())),
    }
    return result


def _page_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lifecycles = _counts(_word(row.get("lifecycle") or row.get("state")) for row in rows)
    return {
        "total": len(rows),
        "by_lifecycle": lifecycles,
        "active": lifecycles.get("active", 0),
        "locked": sum(
            1 for row in rows if bool(row.get("locked")) or bool(row.get("human_locked"))
        ),
    }


def aggregate_knowledge(
    state: RuntimeState,
    *,
    task_id: str = "",
    limit: int = MAX_RECORDS,
) -> dict[str, Any]:
    """Build a bounded, log-safe overview of what the knowledge layer holds.

    ``task_id`` filters evidence, claims and attempt rounds by attempt task id
    and pages by scope graph id. ``limit`` bounds each namespace scan (1..
    ``MAX_RECORDS``); ``truncated`` says whether any namespace exceeded it.
    """

    bound = max(1, min(int(limit), MAX_RECORDS))
    wanted = _text(task_id)

    evidence_rows, cut_evidence = _bounded(
        [row for row in _as_rows(state, EVIDENCE_NAMESPACE) if _matches_task(row, wanted)],
        bound,
    )
    claim_rows, cut_claims = _bounded(
        [row for row in _as_rows(state, CLAIM_NAMESPACE) if _matches_task(row, wanted)],
        bound,
    )
    attempt_rows, cut_attempts = _bounded(
        [row for row in _as_rows(state, ATTEMPT_CHAIN_NAMESPACE) if _matches_task(row, wanted)],
        bound,
    )
    page_rows, cut_pages = _bounded(
        [row for row in _as_rows(state, PAGES_NAMESPACE) if _matches_task(row, wanted, page=True)],
        bound,
    )

    validation_rows = [
        row for row in evidence_rows if _word(row.get("kind")) == "validation"
    ]
    return {
        "schema": SCHEMA,
        "task_id": wanted,
        "stage": describe_stage(stage_from_env())["stage"],
        "truncated": any((cut_evidence, cut_claims, cut_attempts, cut_pages)),
        "scanned": {
            "evidence": len(evidence_rows),
            "claims": len(claim_rows),
            "attempts": len(attempt_rows),
            "pages": len(page_rows),
        },
        "attempts": _attempt_counts(attempt_rows),
        "evidence": _evidence_counts(evidence_rows),
        "claims": _claim_counts(claim_rows),
        "gate": _gate_counts(validation_rows),
        "pages": _page_counts(page_rows),
    }


def render_text(overview: Mapping[str, Any]) -> str:
    """Human-readable rendering of :func:`aggregate_knowledge` (log-safe)."""

    scope = overview.get("task_id") or "(all tasks)"
    lines = [
        f"knowledge overview - {scope} - stage {overview.get('stage', UNKNOWN)}",
        (
            "  attempt rounds: {total} (completed {completed}, failed {failed}, "
            "cleanup_pending {cleanup_pending})"
        ).format(**(overview.get("attempts") or {"total": 0})),
        "  evidence: {} {}".format(
            (overview.get("evidence") or {}).get("total", 0),
            (overview.get("evidence") or {}).get("by_kind", {}),
        ),
        "  claims: {} {}".format(
            (overview.get("claims") or {}).get("total", 0),
            (overview.get("claims") or {}).get("by_status", {}),
        ),
        "  gate: allowed {allowed}, denied {denied} {reasons}".format(
            **(overview.get("gate") or {"allowed": 0, "denied": 0, "reasons": {}})
        ),
        "  pages: {} {}".format(
            (overview.get("pages") or {}).get("total", 0),
            (overview.get("pages") or {}).get("by_lifecycle", {}),
        ),
    ]
    if overview.get("truncated"):
        lines.append("  (truncated by limit)")
    return "\n".join(lines)


def load_state(value: str) -> RuntimeState:
    """Build a state handle from a PostgreSQL URL or a SQLite file path."""

    raw = (value or "").strip()
    if not raw:
        raise ValueError("state path or database URL is required")
    if urlsplit(raw).scheme in ("postgres", "postgresql"):
        return RuntimeState(database_url=raw)
    return RuntimeState(raw)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m ultimate_coders.knowledge.dashboard``."""

    parser = argparse.ArgumentParser(
        prog="python -m ultimate_coders.knowledge.dashboard",
        description="Bounded, log-safe overview of the knowledge layer state.",
    )
    parser.add_argument("--state", required=True, help="SQLite state file or PostgreSQL URL")
    parser.add_argument("--task-id", default="", help="only records of this task/graph")
    parser.add_argument("--limit", type=int, default=MAX_RECORDS, help="rows per namespace")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    overview = aggregate_knowledge(
        load_state(args.state), task_id=args.task_id, limit=args.limit
    )
    if args.json:
        json.dump(overview, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(overview) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI glue
    raise SystemExit(main())
