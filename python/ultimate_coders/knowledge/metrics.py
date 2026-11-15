"""A-B measurement for the knowledge layer (stage B versus stage C).

The acceptance metrics in ``docs/architecture/knowledge-upgrade/spec-p0.md`` are
computed only from authoritative records - attempt patch rounds and the
``ATTEMPT_OUTCOME`` / ``FAILURE_SIGNATURE`` evidence rows. No prompt text, model
narration or retry-event wording feeds a number here (principle A), and a
missing usage number stays "not reported" instead of being counted as zero.

Two shapes are provided:

* :func:`compute_metrics` over one set of attempts (for example one rollout
  stage or one time window);
* :func:`compare_metrics` for the paired stage B -> stage C comparison: tasks
  present in both sets are paired, and the verdict only calls an improvement
  when success is non-inferior and tokens do not get materially worse.

The module also runs as a small CLI so the harness can be executed against a
SQLite state file or a PostgreSQL ``UC_DATABASE_URL`` without importing worker
internals::

    python -m ultimate_coders.knowledge.metrics --state .uc/runtime/state.sqlite3 --json
    python -m ultimate_coders.knowledge.metrics \
        --baseline state-a.sqlite3 --variant state-b.sqlite3 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ultimate_coders.knowledge.attempt_chain import NAMESPACE as ATTEMPT_CHAIN_NAMESPACE
from ultimate_coders.knowledge.evidence import NAMESPACE as EVIDENCE_NAMESPACE
from ultimate_coders.knowledge.model import EvidenceKind, safe_text
from ultimate_coders.runtime_state import RuntimeState

MAX_RECORDS = 5000
#: Tokens may grow by this fraction before the comparison calls it a regression.
TOKEN_TOLERANCE = 0.05
#: How many normalized failure signatures the histogram prints (for calibration).
MAX_SIGNATURES = 50


@dataclass(frozen=True)
class AttemptSample:
    """The minimum facts a metric needs about one attempt."""

    task_id: str
    subtask_id: str
    dispatch_attempt: int
    attempt: int
    success: bool
    signature: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def chain_key(self) -> str:
        return f"{self.task_id}:{self.subtask_id}:{self.dispatch_attempt}"

    @property
    def tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return int(self.input_tokens or 0) + int(self.output_tokens or 0)


@dataclass(frozen=True)
class KnowledgeMetrics:
    """Aggregated knowledge-layer metrics for one sample set."""

    attempts: int = 0
    subtasks: int = 0
    tasks: int = 0
    failures: int = 0
    repeated_failures: int = 0
    succeeded_subtasks: int = 0
    tokens_reported: int = 0
    attempts_with_usage: int = 0

    @property
    def success_rate(self) -> float:
        if not self.subtasks:
            return 0.0
        return self.succeeded_subtasks / self.subtasks

    @property
    def avg_attempts(self) -> float:
        if not self.subtasks:
            return 0.0
        return self.attempts / self.subtasks

    @property
    def repeated_failure_rate(self) -> float:
        if not self.failures:
            return 0.0
        return self.repeated_failures / self.failures

    @property
    def tokens_per_attempt(self) -> float | None:
        if not self.attempts_with_usage:
            return None
        return self.tokens_reported / self.attempts_with_usage

    @property
    def tokens_per_subtask(self) -> float | None:
        if not self.subtasks or not self.attempts_with_usage:
            return None
        return self.tokens_reported / self.subtasks

    def to_dict(self) -> dict[str, Any]:
        """Log-safe metrics payload (counts and rates only, no error text)."""

        return {
            "attempts": self.attempts,
            "subtasks": self.subtasks,
            "tasks": self.tasks,
            "failures": self.failures,
            "repeated_failures": self.repeated_failures,
            "succeeded_subtasks": self.succeeded_subtasks,
            "success_rate": round(self.success_rate, 4),
            "avg_attempts": round(self.avg_attempts, 4),
            "repeated_failure_rate": round(self.repeated_failure_rate, 4),
            "tokens_reported": self.tokens_reported,
            "attempts_with_usage": self.attempts_with_usage,
            "tokens_per_attempt": (
                round(self.tokens_per_attempt, 2)
                if self.tokens_per_attempt is not None
                else None
            ),
            "tokens_per_subtask": (
                round(self.tokens_per_subtask, 2)
                if self.tokens_per_subtask is not None
                else None
            ),
        }


@dataclass(frozen=True)
class ComparisonReport:
    """Paired baseline -> variant comparison with an explicit verdict."""

    baseline: KnowledgeMetrics
    variant: KnowledgeMetrics
    paired_tasks: int
    verdict: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "paired_tasks": self.paired_tasks,
            "reasons": list(self.reasons),
            "baseline": self.baseline.to_dict(),
            "variant": self.variant.to_dict(),
        }


# ---------------------------------------------------------------------------
# Collection (authoritative records only)
# ---------------------------------------------------------------------------


def _attempt_parts(payload: Any) -> tuple[str, str, int, int]:
    """Read the attempt identity from a stored attempt block or key."""

    data = payload if isinstance(payload, Mapping) else {}
    task_id = safe_text(data.get("task_id"), limit=120)
    subtask_id = safe_text(data.get("subtask_id"), limit=120)
    dispatch = data.get("dispatch_attempt", 0)
    attempt = data.get("attempt", 0)
    try:
        dispatch_value = max(0, int(dispatch))
    except (TypeError, ValueError):
        dispatch_value = 0
    try:
        attempt_value = max(0, int(attempt))
    except (TypeError, ValueError):
        attempt_value = 0
    return task_id, subtask_id, dispatch_value, attempt_value


def _usage_from_payload(payload: Any) -> tuple[int | None, int | None]:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    if not isinstance(usage, Mapping):
        return None, None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    input_value = input_tokens if isinstance(input_tokens, int) else None
    output_value = output_tokens if isinstance(output_tokens, int) else None
    return input_value, output_value


def collect_samples(
    state: RuntimeState, *, max_records: int = MAX_RECORDS
) -> list[AttemptSample]:
    """Collect attempt samples from attempt rounds and attempt evidence."""

    cap = max(1, int(max_records))
    attempts: dict[str, dict[str, Any]] = {}
    rounds = state.records(ATTEMPT_CHAIN_NAMESPACE)[:cap]
    for row in rounds:
        if not isinstance(row, Mapping):
            continue
        attempt_payload = row.get("attempt")
        task_id, subtask_id, dispatch, attempt = _attempt_parts(attempt_payload)
        if not subtask_id:
            continue
        key = f"{task_id}:{subtask_id}:{dispatch}:{attempt}"
        attempts[key] = {
            "task_id": task_id,
            "subtask_id": subtask_id,
            "dispatch": dispatch,
            "attempt": attempt,
            "success": False,
            "signature": "",
            "input_tokens": None,
            "output_tokens": None,
        }
    evidence_rows = state.records(EVIDENCE_NAMESPACE)[: cap * 2]
    for row in evidence_rows:
        if not isinstance(row, Mapping):
            continue
        kind = safe_text(row.get("kind"), limit=40)
        attempt_payload = row.get("attempt")
        task_id, subtask_id, dispatch, attempt = _attempt_parts(attempt_payload)
        if not subtask_id:
            continue
        key = f"{task_id}:{subtask_id}:{dispatch}:{attempt}"
        payload = row.get("payload")
        entry = attempts.setdefault(
            key,
            {
                "task_id": task_id,
                "subtask_id": subtask_id,
                "dispatch": dispatch,
                "attempt": attempt,
                "success": False,
                "signature": "",
                "input_tokens": None,
                "output_tokens": None,
            },
        )
        if kind == EvidenceKind.ATTEMPT_OUTCOME.value:
            entry["success"] = (
                bool(payload.get("success")) if isinstance(payload, Mapping) else False
            )
            input_tokens, output_tokens = _usage_from_payload(payload)
            entry["input_tokens"] = input_tokens
            entry["output_tokens"] = output_tokens
        elif kind == EvidenceKind.FAILURE_SIGNATURE.value:
            signature = payload.get("signature") if isinstance(payload, Mapping) else ""
            entry["signature"] = safe_text(signature, limit=200)
    samples = [
        AttemptSample(
            task_id=str(entry["task_id"]),
            subtask_id=str(entry["subtask_id"]),
            dispatch_attempt=int(entry["dispatch"]),
            attempt=int(entry["attempt"]),
            success=bool(entry["success"]),
            signature=str(entry["signature"]),
            input_tokens=entry["input_tokens"],
            output_tokens=entry["output_tokens"],
        )
        for entry in attempts.values()
    ]
    samples.sort(
        key=lambda item: (
            item.task_id,
            item.subtask_id,
            item.dispatch_attempt,
            item.attempt,
        )
    )
    return samples


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def _chain_groups(samples: Sequence[AttemptSample]) -> dict[str, list[AttemptSample]]:
    groups: dict[str, list[AttemptSample]] = {}
    for sample in samples:
        groups.setdefault(sample.chain_key, []).append(sample)
    for chain in groups.values():
        chain.sort(key=lambda item: item.attempt)
    return groups


def compute_metrics(samples: Sequence[AttemptSample]) -> KnowledgeMetrics:
    """Aggregate attempts into the acceptance metrics."""

    groups = _chain_groups(samples)
    tasks = {sample.task_id for sample in samples if sample.task_id}
    failures = 0
    repeated = 0
    succeeded = 0
    tokens = 0
    with_usage = 0
    for chain in groups.values():
        seen_signatures: set[str] = set()
        succeeded += 1 if chain[-1].success else 0
        for sample in chain:
            if sample.tokens is not None:
                tokens += sample.tokens
                with_usage += 1
            if sample.success:
                continue
            failures += 1
            if sample.signature:
                if sample.signature in seen_signatures:
                    repeated += 1
                seen_signatures.add(sample.signature)
    return KnowledgeMetrics(
        attempts=len(samples),
        subtasks=len(groups),
        tasks=len(tasks),
        failures=failures,
        repeated_failures=repeated,
        succeeded_subtasks=succeeded,
        tokens_reported=tokens,
        attempts_with_usage=with_usage,
    )


def _paired_samples(
    baseline: Sequence[AttemptSample], variant: Sequence[AttemptSample]
) -> tuple[list[AttemptSample], list[AttemptSample], int]:
    baseline_tasks = {sample.task_id for sample in baseline if sample.task_id}
    variant_tasks = {sample.task_id for sample in variant if sample.task_id}
    shared = baseline_tasks & variant_tasks
    if not shared:
        return [], [], 0
    left = [sample for sample in baseline if sample.task_id in shared]
    right = [sample for sample in variant if sample.task_id in shared]
    return left, right, len(shared)


def _tokens_not_worse(baseline: KnowledgeMetrics, variant: KnowledgeMetrics) -> bool:
    if baseline.tokens_per_subtask is None or variant.tokens_per_subtask is None:
        return True
    return variant.tokens_per_subtask <= baseline.tokens_per_subtask * (1 + TOKEN_TOLERANCE)


def compare_metrics(
    baseline: Sequence[AttemptSample], variant: Sequence[AttemptSample]
) -> ComparisonReport:
    """Compare two sample sets with the paired task rule from spec-p0.md."""

    paired_baseline, paired_variant, paired_tasks = _paired_samples(baseline, variant)
    left = compute_metrics(paired_baseline)
    right = compute_metrics(paired_variant)
    reasons: list[str] = []
    if paired_tasks < 2:
        return ComparisonReport(
            baseline=left,
            variant=right,
            paired_tasks=paired_tasks,
            verdict="insufficient_data",
            reasons=("paired_tasks<2",),
        )
    success_delta = right.success_rate - left.success_rate
    if success_delta < 0:
        return ComparisonReport(
            baseline=left,
            variant=right,
            paired_tasks=paired_tasks,
            verdict="regressed",
            reasons=("success_rate_lower",),
        )
    attempts_reduced = right.avg_attempts < left.avg_attempts
    repeated_reduced = right.repeated_failure_rate < left.repeated_failure_rate
    if attempts_reduced:
        reasons.append("attempts_reduced")
    if repeated_reduced:
        reasons.append("repeated_failures_reduced")
    if not _tokens_not_worse(left, right):
        return ComparisonReport(
            baseline=left,
            variant=right,
            paired_tasks=paired_tasks,
            verdict="inconclusive",
            reasons=tuple(reasons + ["tokens_worse"]),
        )
    if attempts_reduced or repeated_reduced:
        return ComparisonReport(
            baseline=left,
            variant=right,
            paired_tasks=paired_tasks,
            verdict="improved",
            reasons=tuple(reasons),
        )
    return ComparisonReport(
        baseline=left,
        variant=right,
        paired_tasks=paired_tasks,
        verdict="non_inferior",
        reasons=tuple(reasons),
    )


def signature_histogram(
    samples: Sequence[AttemptSample], *, limit: int = MAX_SIGNATURES
) -> list[dict[str, Any]]:
    """Count normalized failure signatures (calibration input, never a verdict).

    The output is what an operator needs to calibrate ``normalize_failure_signature``:
    if many different stderr texts collapse into one signature the metric is too
    coarse, and if one root cause splits into many signatures it is too fine.
    Rows are ordered by count then signature, and the signature text is bounded
    by the normalizer already, so printing it is log-safe.
    """

    effective = max(0, min(int(limit or 0), MAX_SIGNATURES))
    if effective == 0:
        return []
    counts: dict[str, int] = {}
    for sample in samples:
        if sample.success or not sample.signature:
            continue
        counts[sample.signature] = counts.get(sample.signature, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"signature": signature, "count": count} for signature, count in ordered[:effective]]


# ---------------------------------------------------------------------------
# CLI harness
# ---------------------------------------------------------------------------


def load_state(value: str) -> RuntimeState:
    """Build a state handle from a SQLite path or a database URL."""

    text = (value or "").strip()
    if text.startswith(("postgres://", "postgresql://")):
        return RuntimeState(database_url=text)
    return RuntimeState(Path(text))


def _format_metrics(metrics: KnowledgeMetrics) -> str:
    payload = metrics.to_dict()
    lines = ["metric value"]
    for key in (
        "attempts",
        "subtasks",
        "tasks",
        "success_rate",
        "avg_attempts",
        "failures",
        "repeated_failures",
        "repeated_failure_rate",
        "tokens_reported",
        "tokens_per_subtask",
    ):
        lines.append(f"{key} {payload[key]}")
    return "\n".join(lines)


def _format_signatures(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "signatures none (no failed attempt reported a signature)"
    lines = ["signature count"]
    for row in rows:
        lines.append(f"{row['signature']} {row['count']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the measurement harness (single state or paired A/B states)."""

    parser = argparse.ArgumentParser(prog="knowledge-metrics")
    parser.add_argument("--state", default="", help="SQLite state file or database URL")
    parser.add_argument("--baseline", default="", help="baseline SQLite state file or URL")
    parser.add_argument("--variant", default="", help="variant SQLite state file or URL")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--signatures",
        action="store_true",
        help="include the normalized failure-signature histogram (calibration)",
    )
    parser.add_argument("--max-records", type=int, default=MAX_RECORDS)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.state:
        samples = collect_samples(load_state(args.state), max_records=args.max_records)
        metrics = compute_metrics(samples)
        payload: dict[str, Any] = metrics.to_dict()
        histogram = signature_histogram(samples) if args.signatures else []
        if args.signatures:
            payload["signatures"] = histogram
        if args.json:
            print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
        else:
            print(_format_metrics(metrics))
            if args.signatures:
                print(_format_signatures(histogram))
        return 0
    if not args.baseline or not args.variant:
        parser.print_usage(sys.stderr)
        print("error: provide --state or both --baseline and --variant", file=sys.stderr)
        return 2
    baseline_samples = collect_samples(
        load_state(args.baseline), max_records=args.max_records
    )
    variant_samples = collect_samples(
        load_state(args.variant), max_records=args.max_records
    )
    report = compare_metrics(baseline_samples, variant_samples)
    if args.json:
        payload = report.to_dict()
        if args.signatures:
            payload["signatures"] = {
                "baseline": signature_histogram(baseline_samples),
                "variant": signature_histogram(variant_samples),
            }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print(f"verdict {report.verdict} paired_tasks {report.paired_tasks}")
        print(f"reasons {','.join(report.reasons) or 'none'}")
        print("baseline:")
        print(_format_metrics(report.baseline))
        print("variant:")
        print(_format_metrics(report.variant))
        if args.signatures:
            print("baseline signatures:")
            print(_format_signatures(signature_histogram(baseline_samples)))
            print("variant signatures:")
            print(_format_signatures(signature_histogram(variant_samples)))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via ``main(argv)``
    raise SystemExit(main())
