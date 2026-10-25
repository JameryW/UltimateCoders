"""Approx-eval rank-agreement gate and arming latch (Agent Trust P1 / T7).

Pure agreement surface: Spearman ρ on parallel score sequences, optional arming
latch. No Oracle subclass, no uplift fields, no candidate prune (ticket 08).
Truth rankings must come from report-role hold-out; callers may pass accompanying
result objects for identity checks when eval-role enforce is on.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .models import require_report_role

APPROX_EVAL_ENFORCE_ENV = "UC_APPROX_EVAL_ENFORCE"
APPROX_EVAL_ARMED_ENV = "UC_APPROX_EVAL_ARMED"

DEFAULT_SPEARMAN_THRESHOLD = 0.8

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def _env_flag_enabled(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY_ENV


def approx_eval_enforce_enabled() -> bool:
    """Whether approx-eval enforce is on; default off."""
    return _env_flag_enabled(APPROX_EVAL_ENFORCE_ENV)


def approx_eval_armed() -> bool:
    """Whether the agreement arming latch is set; default off."""
    return _env_flag_enabled(APPROX_EVAL_ARMED_ENV)


def approx_eval_ready_to_prune() -> bool:
    """True only when enforce is on AND the latch is armed (for ticket 08)."""
    return approx_eval_enforce_enabled() and approx_eval_armed()


@dataclass(frozen=True)
class RankAgreementOutcome:
    passed: bool
    metric: str
    coefficient: float
    threshold: float
    n: int
    reasons: list[str]


def _average_ranks(values: Sequence[float]) -> list[float]:
    """1-based midranks; ties share the average of their ordinal positions."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    den_y = sum((y - mean_y) ** 2 for y in ys)
    if den_x == 0.0 or den_y == 0.0:
        return None
    return num / math.sqrt(den_x * den_y)


def _fail(
    *,
    metric: str,
    threshold: float,
    n: int,
    reasons: list[str],
    coefficient: float = float("nan"),
) -> RankAgreementOutcome:
    return RankAgreementOutcome(
        passed=False,
        metric=metric,
        coefficient=coefficient,
        threshold=threshold,
        n=n,
        reasons=reasons,
    )


def evaluate_rank_agreement(
    truth_scores: Sequence[float],
    approx_scores: Sequence[float],
    *,
    metric: str = "spearman",
    threshold: float = DEFAULT_SPEARMAN_THRESHOLD,
    truth_results: Sequence[Any] | None = None,
) -> RankAgreementOutcome:
    """Spearman ρ between truth and approx score sequences.

    Callers must supply rankings derived from report-role hold-out. When
    ``truth_results`` is provided and eval-role (or report-uplift) enforce is on,
    each accompanying object must pass :func:`require_report_role`.
    """
    metric_key = (metric or "").strip().lower()
    n_truth = len(truth_scores)
    n_approx = len(approx_scores)
    n = min(n_truth, n_approx)

    if truth_results is not None:
        if len(truth_results) != n_truth:
            return _fail(
                metric=metric_key or metric,
                threshold=threshold,
                n=n_truth,
                reasons=["truth_results length mismatch"],
            )
        try:
            for item in truth_results:
                require_report_role(item)
        except ValueError as exc:
            return _fail(
                metric=metric_key or metric,
                threshold=threshold,
                n=n_truth,
                reasons=[f"report identity required: {exc}"],
            )

    if metric_key != "spearman":
        return _fail(
            metric=metric_key or str(metric),
            threshold=threshold,
            n=n,
            reasons=[f"unsupported metric: {metric!r} (locked to spearman)"],
        )

    if n_truth != n_approx:
        return _fail(
            metric=metric_key,
            threshold=threshold,
            n=n,
            reasons=[
                f"length mismatch: truth={n_truth} approx={n_approx}",
            ],
        )

    if n < 3:
        return _fail(
            metric=metric_key,
            threshold=threshold,
            n=n,
            reasons=[f"n={n} < 3; Spearman undefined"],
        )

    truth: list[float] = []
    approx: list[float] = []
    for i, (t, a) in enumerate(zip(truth_scores, approx_scores)):
        try:
            tf = float(t)
            af = float(a)
        except (TypeError, ValueError):
            return _fail(
                metric=metric_key,
                threshold=threshold,
                n=n,
                reasons=[f"non-numeric score at index {i}"],
            )
        if not math.isfinite(tf) or not math.isfinite(af):
            return _fail(
                metric=metric_key,
                threshold=threshold,
                n=n,
                reasons=[f"non-finite score at index {i}"],
            )
        truth.append(tf)
        approx.append(af)

    truth_ranks = _average_ranks(truth)
    approx_ranks = _average_ranks(approx)
    rho = _pearson(truth_ranks, approx_ranks)
    if rho is None:
        return _fail(
            metric=metric_key,
            threshold=threshold,
            n=n,
            reasons=["Spearman undefined for constant sequence"],
        )

    passed = rho >= threshold
    reasons: list[str] = []
    if not passed:
        reasons.append(f"spearman rho={rho:.6g} < threshold={threshold}")
    return RankAgreementOutcome(
        passed=passed,
        metric=metric_key,
        coefficient=rho,
        threshold=threshold,
        n=n,
        reasons=reasons,
    )


def arm_approx_eval(outcome: RankAgreementOutcome) -> bool:
    """Set the arming latch only when ``outcome.passed``; otherwise disarm.

    Returns whether the latch is armed after the call.
    """
    if outcome.passed:
        os.environ[APPROX_EVAL_ARMED_ENV] = "1"
        return True
    disarm_approx_eval()
    return False


def disarm_approx_eval() -> None:
    """Clear the approx-eval arming latch."""
    os.environ.pop(APPROX_EVAL_ARMED_ENV, None)
