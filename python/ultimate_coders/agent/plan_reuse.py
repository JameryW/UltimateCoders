"""Reuse a prior attempt's repair trajectory as a plan (ledger item #74 wiring).

``search/stair_tree.py`` can mix strategy, subgoal, and diagnostic nodes from
historical trajectories into one executable plan, but nothing built the tree
from real evidence.  This module is that caller: on a retry the worker already
holds the previous attempts (patch rounds with an outcome, touched files, a
failure summary, and a test verdict), which is exactly a repair trajectory.

The mapping is deterministic and deliberately shallow:

* the attempt's summary or first failure line becomes the strategy node;
* each touched file becomes a subgoal ("edit <path>");
* the failure's diagnostic lines become diagnostic nodes, carrying the test
  verdict as a tag.

Failed attempts are included on purpose: on a retry the useful information is
what was already tried and where it broke.  The rendered plan is additive
evidence for the next prompt — it never replaces the raw recovery evidence, and
it never claims a repair succeeded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ultimate_coders.search.stair_tree import (
    StairNode,
    StairPlan,
    StairTree,
    TrajectoryLevel,
    stair_tree_enabled,
)

__all__ = [
    "PLAN_REUSE_ENV",
    "MAX_ATTEMPTS",
    "MAX_PLAN_NODES",
    "AttemptSketch",
    "sketch_from_round",
    "plan_block",
    "plan_reuse_enabled",
]

PLAN_REUSE_ENV = "UC_PLAN_REUSE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_ATTEMPTS = 6
MAX_PLAN_NODES = 5
_MAX_TEXT = 160
_MAX_DIAG = 120
_FAILED_TAGS = ("failure",)
_MAX_SUBGOALS = 3
_MAX_DIAGNOSTICS = 2

_SUCCESS_OUTCOMES = frozenset(
    {"success", "succeeded", "resolved", "passed", "delivered", "complete", "completed"}
)


def plan_reuse_enabled() -> bool:
    """True only when the plan reuse flag and the STAIR tree are both on."""
    if os.environ.get(PLAN_REUSE_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return stair_tree_enabled()


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned[:limit].rstrip()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _attempt_number(round_obj: object) -> int:
    ref = getattr(round_obj, "attempt", None)
    for name in ("attempt", "dispatch_attempt"):
        raw = getattr(ref, name, None)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return 0


@dataclass(frozen=True)
class AttemptSketch:
    """One attempt reduced to the three STAIR levels."""

    attempt: int
    success: bool
    strategy: str
    files: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy.strip():
            raise ValueError("strategy must not be empty")


def sketch_from_round(round_obj: object) -> AttemptSketch | None:
    """Project an attempt round onto a plan sketch, or ``None`` when empty."""
    outcome = _text(getattr(round_obj, "outcome", "")).lower()
    summary = _text(getattr(round_obj, "summary", ""))
    failure = _text(getattr(round_obj, "failure", ""))
    lines = [line for line in (part.strip() for part in failure.splitlines()) if line]
    verdict = _text(getattr(round_obj, "test_verdict", ""))

    strategy = summary or (lines[0] if lines else "")
    if not strategy:
        return None
    label = f"attempt {_attempt_number(round_obj)} {outcome or 'unknown'}"
    strategy = f"{label}: {_clip(strategy, _MAX_TEXT)}"

    files: list[str] = []
    for patch in getattr(round_obj, "patches", ()) or ():
        path = _text(getattr(patch, "path", ""))
        if path and path not in files:
            files.append(path)

    # Without a summary the first failure line is already the strategy, so it
    # is not repeated as a diagnostic; otherwise every failure line is worth
    # keeping, because the retry needs to know what broke.
    diagnostic_lines = lines[1:] if not summary else lines
    diagnostics = [_clip(line, _MAX_DIAG) for line in diagnostic_lines][
        :_MAX_DIAGNOSTICS
    ]
    if verdict:
        diagnostics.append(_clip(f"tests: {verdict}", _MAX_DIAG))

    return AttemptSketch(
        attempt=_attempt_number(round_obj),
        success=outcome in _SUCCESS_OUTCOMES,
        strategy=strategy,
        files=tuple(files[:_MAX_SUBGOALS]),
        diagnostics=tuple(diagnostics),
    )


def _build_tree(sketches: tuple[AttemptSketch, ...]) -> StairTree:
    tree = StairTree()
    for sketch in sketches:
        tags = ("success",) if sketch.success else _FAILED_TAGS
        tree.add_trajectory(
            sketch.strategy,
            [f"edit {path}" for path in sketch.files],
            list(sketch.diagnostics),
            tags=tags,
            success=sketch.success,
        )
    return tree


def _render(plan: StairPlan) -> str:
    lines: list[str] = []
    if plan.strategy is not None:
        lines.append(f"What was tried: {plan.strategy.text}")
    for node in plan.subgoals:
        lines.append(f"Next: {node.text}")
    for node in plan.diagnostics:
        lines.append(f"Known failure: {node.text}")
    return "\n".join(lines)


def plan_block(
    query: str,
    rounds: object,
    *,
    max_nodes: int = MAX_PLAN_NODES,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[str, dict[str, object]] | None:
    """Render a bounded repair plan for ``query`` from prior attempt rounds."""
    items = list(rounds or ())
    if not items:
        return None
    sketches: list[AttemptSketch] = []
    for round_obj in items[-max_attempts:]:
        sketch = sketch_from_round(round_obj)
        if sketch is not None:
            sketches.append(sketch)
    if not sketches:
        return None
    tree = _build_tree(tuple(sketches))
    plan = tree.search(query, max_nodes=max_nodes, include_failed=True)
    if plan.strategy is None and not plan.subgoals and not plan.diagnostics:
        return None
    rendered = _render(plan)
    if not rendered.strip():
        return None
    meta: dict[str, object] = {
        "attempts": len(sketches),
        "nodes": len(plan.nodes),
        "score": plan.score,
        "strategy": plan.strategy.text if plan.strategy is not None else "",
        "failed_attempts": sum(1 for sketch in sketches if not sketch.success),
    }
    return rendered, meta


def plan_tags(plan: StairPlan) -> tuple[str, ...]:
    """Deterministic tag list for a rendered plan (useful in event payloads)."""
    tags: list[str] = []
    for node in plan.nodes:
        for tag in node.tags:
            if tag not in tags:
                tags.append(tag)
    return tuple(tags)


def node_levels(nodes: tuple[StairNode, ...]) -> dict[str, int]:
    """Count nodes per level, so a caller can report the mix it rendered."""
    counts = {level.value: 0 for level in TrajectoryLevel}
    for node in nodes:
        counts[node.level.value] += 1
    return counts
