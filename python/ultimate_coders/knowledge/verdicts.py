"""Deterministic extraction of verification verdicts from worker outputs.

Principle A of the evidence-first design: record only what a backend actually
reported. A missing or ambiguous verdict becomes ``""`` (unknown) - never a
fabricated pass or fail. The extractor therefore reads the structured carriers
that already exist on a subtask result (``SubtaskResult.review`` and the
domain-result blocks a sandbox writes back) and leaves everything else alone.

This module is pure: it neither persists nor mutates. ``EvidenceLedger`` owns
the durable record, and the worker decides which verdicts to record.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ultimate_coders.knowledge.model import safe_text

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
ORACLE_ACCEPTED = "accepted"
ORACLE_REJECTED = "rejected"
TEST_PASSED = "passed"
TEST_FAILED = "failed"

MAX_ISSUES = 5
MAX_REASONS = 5
MAX_TEXT = 300
MAX_SUMMARY = 500

_POSITIVE_WORDS = frozenset(
    {"accepted", "accept", "passed", "pass", "ok", "true", "yes", "success"}
)
_NEGATIVE_WORDS = frozenset(
    {"rejected", "reject", "failed", "fail", "error", "false", "no"}
)


@dataclass(frozen=True)
class VerificationReport:
    """The verification facts one attempt actually produced.

    Empty strings mean "the backend did not report a verdict"; they are not
    implicit failures. ``round_verdict()`` renders the compact label stored on
    an ``AttemptRound`` so a recovery view can show what was verified without
    replaying the whole evidence ledger.
    """

    review_verdict: str = ""
    review_issues: tuple[str, ...] = ()
    oracle_verdict: str = ""
    oracle_reasons: tuple[str, ...] = ()
    test_verdict: str = ""
    test_summary: str = ""

    def has_any(self) -> bool:
        """Whether any verdict was actually reported."""

        return bool(self.review_verdict or self.oracle_verdict or self.test_verdict)

    def round_verdict(self) -> str:
        """Compact, bounded label for ``AttemptRound.test_verdict``.

        Deliberately says ``unknown`` rather than guessing: a round with no
        reported verification must not look verified in a recovery view.
        """

        parts = []
        if self.review_verdict:
            parts.append(f"review={self.review_verdict}")
        if self.oracle_verdict:
            parts.append(f"oracle={self.oracle_verdict}")
        if self.test_verdict:
            parts.append(f"test={self.test_verdict}")
        return " ".join(parts) if parts else "unknown"


def extract_verification(
    *,
    review: Any = None,
    domain_result: Any = None,
) -> VerificationReport:
    """Extract the verification report from a result's structured carriers.

    Tolerant by design: an object or mapping is accepted for the review, and
    oracle/test facts are read from a ``domain_result`` mapping (or object with
    equivalent attributes). Unknown shapes are ignored - never guessed.
    """

    review_verdict, review_issues = _extract_review(review)
    oracle_verdict, oracle_reasons = _extract_oracle(domain_result)
    test_verdict, test_summary = _extract_tests(domain_result)
    return VerificationReport(
        review_verdict=review_verdict,
        review_issues=review_issues,
        oracle_verdict=oracle_verdict,
        oracle_reasons=oracle_reasons,
        test_verdict=test_verdict,
        test_summary=test_summary,
    )


def _extract_review(review: Any) -> tuple[str, tuple[str, ...]]:
    if review is None:
        return "", ()
    approved = _as_bool(_field(review, "approved", "success", "accepted"))
    if approved is None:
        return "", ()
    issues = _text_tuple(_field(review, "issues", "problems"), MAX_ISSUES)
    return (REVIEW_APPROVED if approved else REVIEW_REJECTED), issues


def _extract_oracle(domain_result: Any) -> tuple[str, tuple[str, ...]]:
    oracle = _field(domain_result, "oracle")
    if oracle is None:
        return "", ()
    accepted = _as_bool(_field(oracle, "accepted", "success", "passed", "ok"))
    if accepted is None:
        accepted = _word_to_bool(_as_text(_field(oracle, "verdict", "result", "status")))
    if accepted is None:
        return "", ()
    reasons = _text_tuple(_field(oracle, "reasons", "issues", "failures"), MAX_REASONS)
    return (ORACLE_ACCEPTED if accepted else ORACLE_REJECTED), reasons


def _extract_tests(domain_result: Any) -> tuple[str, str]:
    explicit = _as_text(_field(domain_result, "test_verdict", "tests_verdict"))
    verdict = _word_to_test_verdict(explicit)
    if verdict:
        return verdict, ""

    candidates: Iterable[Any] = (
        _field(domain_result, "tests", "test_results"),
        _field(domain_result, "verification", "verify"),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        passed = _as_bool(_field(candidate, "passed", "ok", "success", "accepted"))
        if passed is None:
            passed = _word_to_bool(
                _as_text(_field(candidate, "verdict", "result", "status"))
            )
        if passed is None:
            continue
        summary = safe_text(
            _as_text(_field(candidate, "summary", "message", "notes")), limit=MAX_SUMMARY
        )
        return (TEST_PASSED if passed else TEST_FAILED), summary

    passed = _as_bool(_field(domain_result, "verification_passed"))
    if passed is not None:
        return (TEST_PASSED if passed else TEST_FAILED), ""
    return "", ""


def _field(value: Any, *names: str) -> Any:
    """Read the first present field from a mapping or an object, else ``None``."""

    if value is None:
        return None
    mapping = value if isinstance(value, Mapping) else None
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
        return None
    for name in names:
        try:
            return getattr(value, name)
        except Exception:  # pragma: no cover - attribute access is not data
            continue
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _word_to_bool(value)
    return None


def _word_to_bool(value: str) -> bool | None:
    word = value.strip().lower()
    if not word:
        return None
    if word in _POSITIVE_WORDS:
        return True
    if word in _NEGATIVE_WORDS:
        return False
    return None


def _word_to_test_verdict(value: str) -> str:
    word = value.strip().lower()
    if not word:
        return ""
    if word in _POSITIVE_WORDS:
        return TEST_PASSED
    if word in _NEGATIVE_WORDS:
        return TEST_FAILED
    return ""


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value)


def _text_tuple(value: Any, limit: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return ()
    texts = []
    for item in items[:limit]:
        text = safe_text(_as_text(item), limit=MAX_TEXT).strip()
        if text:
            texts.append(text)
    return tuple(texts)
