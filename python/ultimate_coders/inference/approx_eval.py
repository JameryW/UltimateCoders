"""Approx-eval rank-agreement gate, prune-only path, and auto-disable (P1 / T7–T8).

Agreement (T7): Spearman ρ on parallel score sequences plus an arming latch.
Prune (T8): discard/keep decisions only when enforce AND armed; never an
OracleVerdict or public uplift payload. Bench/harness fingerprint change
re-runs agreement and disarms on failure.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import require_report_role

APPROX_EVAL_ENFORCE_ENV = "UC_APPROX_EVAL_ENFORCE"
APPROX_EVAL_ARMED_ENV = "UC_APPROX_EVAL_ARMED"

DEFAULT_SPEARMAN_THRESHOLD = 0.8
DEFAULT_MAX_DISCARD_FRACTION = 0.5

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
    """True only when enforce is on AND the latch is armed."""
    return approx_eval_enforce_enabled() and approx_eval_armed()


@dataclass(frozen=True)
class RankAgreementOutcome:
    passed: bool
    metric: str
    coefficient: float
    threshold: float
    n: int
    reasons: list[str]


@dataclass(frozen=True)
class PruneDecision:
    """Per-candidate keep/discard from the approx pre-filter (never an Oracle verdict)."""

    index: int
    keep: bool
    approx_score: float
    reason: str
    item_id: str | None = None


@dataclass(frozen=True)
class PruneResult:
    """Prune-only outcome: survivors for full Oracle / BenchmarkRunner."""

    kept: list[Any]
    kept_indices: list[int]
    decisions: list[PruneDecision]
    armed: bool
    discarded_indices: list[int] = field(default_factory=list)


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


def _coerce_score(value: Any, index: int) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric approx score at index {index}") from exc
    if isinstance(value, bool) or not math.isfinite(score):
        raise ValueError(f"non-finite approx score at index {index}")
    return score


def _item_id(item: Any) -> str | None:
    if isinstance(item, dict):
        raw = item.get("id")
        return raw if isinstance(raw, str) else None
    raw = getattr(item, "id", None)
    return raw if isinstance(raw, str) else None


def prune_candidates(
    items: Sequence[Any],
    scores: Sequence[float],
    *,
    keep_top_k: int | None = None,
    max_discard_fraction: float = DEFAULT_MAX_DISCARD_FRACTION,
    higher_is_better: bool = True,
) -> PruneResult:
    """Rank-order prune: keep/discard only; never an OracleVerdict or uplift.

    When :func:`approx_eval_ready_to_prune` is false, every candidate is kept
    (pass-through). When armed, candidates are ordered by ``scores``
    (``higher_is_better`` controls direction; use ``False`` for latency such as
    ``exec_time_ms``) and only the top-k survive for full Oracle /
    BenchmarkRunner. Default ``keep_top_k`` is
    ``max(1, ceil(n * (1 - max_discard_fraction)))``.
    """
    n = len(items)
    if len(scores) != n:
        raise ValueError(f"items/scores length mismatch: items={n} scores={len(scores)}")

    parsed: list[float] = [_coerce_score(scores[i], i) for i in range(n)]

    if not approx_eval_ready_to_prune():
        decisions = [
            PruneDecision(
                index=i,
                keep=True,
                approx_score=parsed[i],
                reason="approx prune not ready; keep all",
                item_id=_item_id(items[i]),
            )
            for i in range(n)
        ]
        return PruneResult(
            kept=list(items),
            kept_indices=list(range(n)),
            decisions=decisions,
            armed=False,
            discarded_indices=[],
        )

    if not math.isfinite(max_discard_fraction) or not (0.0 <= max_discard_fraction < 1.0):
        raise ValueError("max_discard_fraction must be in [0, 1)")

    if keep_top_k is None:
        keep_n = max(1, math.ceil(n * (1.0 - max_discard_fraction))) if n else 0
    else:
        if not isinstance(keep_top_k, int) or isinstance(keep_top_k, bool):
            raise ValueError("keep_top_k must be a positive int or None")
        if keep_top_k < 1:
            raise ValueError("keep_top_k must be >= 1")
        keep_n = min(keep_top_k, n) if n else 0

    # Stable rank: better scores first; ties keep original index order.
    ranked = sorted(
        range(n),
        key=lambda i: ((-parsed[i] if higher_is_better else parsed[i]), i),
    )
    keep_set = set(ranked[:keep_n])
    decisions: list[PruneDecision] = []
    for i in range(n):
        keep = i in keep_set
        decisions.append(
            PruneDecision(
                index=i,
                keep=keep,
                approx_score=parsed[i],
                reason="approx top-k keep" if keep else "approx prune discard",
                item_id=_item_id(items[i]),
            )
        )
    kept_indices = [i for i in ranked if i in keep_set]
    discarded_indices = [i for i in ranked if i not in keep_set]
    return PruneResult(
        kept=[items[i] for i in kept_indices],
        kept_indices=kept_indices,
        decisions=decisions,
        armed=True,
        discarded_indices=discarded_indices,
    )


def refuse_approx_sourced_uplift(payload: Any) -> None:
    """Reject approx-sourced numbers at the public uplift boundary.

    Raises ``ValueError`` when ``payload`` (or nested ``public_uplift`` /
    ``report_verdict``) carries ``source == "approx"`` or an ``approx_score``
    field masquerading as uplift. Always active: approx scores must never
    populate published uplift figures.
    """
    if payload is None:
        return
    if not isinstance(payload, dict):
        return
    source = payload.get("source")
    if isinstance(source, str) and source.strip().lower() == "approx":
        raise ValueError("approx-sourced uplift refused (source=approx)")
    if "approx_score" in payload:
        raise ValueError("approx_score cannot populate public uplift")
    for nested_key in ("public_uplift", "report_verdict"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            refuse_approx_sourced_uplift(nested)


def retest_agreement_on_change(
    fingerprint: str,
    truth_scores: Sequence[float],
    approx_scores: Sequence[float],
    *,
    previous_fingerprint: str | None = None,
    **agreement_kwargs: Any,
) -> RankAgreementOutcome:
    """Re-run rank agreement when the bench/harness fingerprint changes.

    Contract:

    - If ``previous_fingerprint`` is not ``None`` and equals ``fingerprint``,
      identity is unchanged: return a synthetic passed outcome and **do not**
      touch the arming latch.
    - Otherwise (cold start when ``previous_fingerprint`` is ``None``, or a
      real fingerprint change): call :func:`evaluate_rank_agreement`. On
      failure call :func:`disarm_approx_eval`; on pass call
      :func:`arm_approx_eval`.

    Callers persist the current fingerprint for the next comparison. This
    helper does not store fingerprints itself.
    """
    threshold = float(agreement_kwargs.get("threshold", DEFAULT_SPEARMAN_THRESHOLD))
    if previous_fingerprint is not None and previous_fingerprint == fingerprint:
        n = min(len(truth_scores), len(approx_scores))
        return RankAgreementOutcome(
            passed=True,
            metric="unchanged",
            coefficient=1.0,
            threshold=threshold,
            n=n,
            reasons=["fingerprint unchanged; latch untouched"],
        )

    outcome = evaluate_rank_agreement(truth_scores, approx_scores, **agreement_kwargs)
    if outcome.passed:
        arm_approx_eval(outcome)
    else:
        disarm_approx_eval()
    return outcome


def filter_ranked_library(
    library: Sequence[Any],
    *,
    score_key: str = "exec_time_ms",
    higher_is_better: bool = False,
    keep_top_k: int | None = None,
    max_discard_fraction: float = DEFAULT_MAX_DISCARD_FRACTION,
) -> list[Any]:
    """Soft MetaInfer pre-filter: prune when armed; otherwise return ``library``.

    Expects a list of dicts with a numeric ``score_key`` (default latency
    ``exec_time_ms``, so ``higher_is_better=False``). Missing/invalid scores
    yield a soft no-op (original order preserved). Survivors are best-first.
    """
    if not isinstance(library, Sequence) or isinstance(library, (str, bytes)):
        return list(library) if library is not None else []
    items = list(library)
    if not items or not approx_eval_ready_to_prune():
        return items
    scores: list[float] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return items
        raw = item.get(score_key)
        try:
            scores.append(_coerce_score(raw, i))
        except ValueError:
            return items
    result = prune_candidates(
        items,
        scores,
        keep_top_k=keep_top_k,
        max_discard_fraction=max_discard_fraction,
        higher_is_better=higher_is_better,
    )
    return list(result.kept)
