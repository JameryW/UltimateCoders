"""Typed reviewers fail-closed (ticket 09 / D5; T10 threshold enablement).

Flag default off; no labels ⇒ disabled; labels without CV threshold ⇒ disabled;
taxonomy uses routing FailureKind; parse_review JSON gate unchanged; no
calibrated 0.5 default; score≥threshold path sets review_verified for completion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from ultimate_coders.agent.completion_conditions import (
    CompletionCondition,
    evaluate_completion_conditions,
)
from ultimate_coders.agent.review import parse_review
from ultimate_coders.agent.routing import FailureKind
from ultimate_coders.agent.typed_review import (
    TYPED_REVIEW_ENFORCE_ENV,
    ThresholdArtifact,
    TypedReviewOutcome,
    apply_typed_review,
    clear_labels,
    clear_thresholds,
    completion_evidence_from_outcome,
    labels_available,
    register_labels,
    register_threshold,
    review_for_kind,
    typed_review_enforce_enabled,
)

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "python" / "ultimate_coders" / "agent" / "typed_review.py"
)


def _schema_threshold(*, threshold: float = 0.82) -> ThresholdArtifact:
    return ThresholdArtifact(
        kind="schema",
        metric="f1",
        threshold=threshold,
        cv_folds=5,
        n_labels=40,
        generated_at="2026-10-25T00:00:00Z",
    )


def _auth_threshold(*, threshold: float = 0.75) -> ThresholdArtifact:
    return ThresholdArtifact(
        kind="auth",
        metric="f1",
        threshold=threshold,
        cv_folds=5,
        n_labels=20,
        generated_at="2026-10-25T00:00:00Z",
    )


def _unknown_threshold(*, threshold: float = 0.7) -> ThresholdArtifact:
    return ThresholdArtifact(
        kind="unknown",
        metric="f1",
        threshold=threshold,
        cv_folds=3,
        n_labels=10,
        generated_at="2026-10-25T00:00:00Z",
    )


def _transient_threshold(*, threshold: float = 0.7) -> ThresholdArtifact:
    return ThresholdArtifact(
        kind="transient",
        metric="f1",
        threshold=threshold,
        cv_folds=3,
        n_labels=10,
        generated_at="2026-10-25T00:00:00Z",
    )


@pytest.fixture(autouse=True)
def _reset_typed_review_state(monkeypatch):
    clear_labels()
    clear_thresholds()
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    yield
    clear_labels()
    clear_thresholds()


# ── feature flag ───────────────────────────────────────────────────


def test_typed_review_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    assert typed_review_enforce_enabled() is False


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off"])
def test_typed_review_flag_falsey_is_off(monkeypatch, value):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, value)
    assert typed_review_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_typed_review_flag_truthy_is_on(monkeypatch, value):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, value)
    assert typed_review_enforce_enabled() is True


# ── taxonomy / FailureKind alignment ───────────────────────────────


def test_taxonomy_uses_routing_failure_kind_values():
    values = {k.value for k in FailureKind}
    assert values == {"transient", "auth", "schema", "unknown"}
    for kind in FailureKind:
        assert labels_available(kind) is False
        outcome = review_for_kind(kind, {})
        assert outcome.kind == kind.value


def test_no_parallel_failure_kind_enum_in_typed_review_module():
    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert "FailureKind" not in class_names
    assert "TypedReviewOutcome" in class_names
    assert "from ultimate_coders.agent.routing import FailureKind" in source


# ── labels registry ────────────────────────────────────────────────


def test_labels_empty_by_default():
    for kind in FailureKind:
        assert labels_available(kind) is False


def test_register_labels_enables_kind_only():
    register_labels(FailureKind.AUTH, ["lab-a", "lab-b"])
    assert labels_available(FailureKind.AUTH) is True
    assert labels_available("auth") is True
    assert labels_available(FailureKind.TRANSIENT) is False
    assert labels_available(FailureKind.SCHEMA) is False


def test_register_empty_labels_disables_kind():
    register_labels("schema", ["x"])
    assert labels_available("schema") is True
    register_labels("schema", [])
    assert labels_available("schema") is False


def test_clear_labels_resets_registry():
    register_labels(FailureKind.UNKNOWN, ["u1"])
    clear_labels()
    assert labels_available(FailureKind.UNKNOWN) is False


# ── review_for_kind fail-closed ────────────────────────────────────


def test_review_disabled_when_flag_off(monkeypatch):
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    register_labels(FailureKind.TRANSIENT, ["t1"])
    register_threshold(_transient_threshold())
    outcome = review_for_kind(FailureKind.TRANSIENT, {"typed_review_score": 0.99})
    assert outcome == TypedReviewOutcome(
        kind="transient",
        enabled=False,
        passed=False,
        reasons=[f"{TYPED_REVIEW_ENFORCE_ENV} is off"],
        review_verified=False,
    )


def test_review_disabled_when_no_labels(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_threshold(_schema_threshold())
    outcome = review_for_kind("schema", {"typed_review_score": 0.99})
    assert outcome.enabled is False
    assert outcome.passed is False
    assert outcome.review_verified is False
    assert "no labels" in outcome.reasons[0]


def test_review_disabled_when_labels_but_no_threshold(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.AUTH, ["a1"])
    outcome = review_for_kind(FailureKind.AUTH, {"typed_review_score": 0.99})
    assert outcome.enabled is False
    assert outcome.passed is False
    assert outcome.review_verified is False
    assert "threshold artifact" in outcome.reasons[0]


def test_review_fails_without_score_meeting_threshold(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.AUTH, ["a1"])
    register_threshold(_auth_threshold(threshold=0.75))
    for evidence in (
        {},
        {"typed_review_score": 0.74},
        {"typed_review_score": False},
        {"typed_review_score": "0.9"},
        {"typed_review_pass": True},
        {"approved": True},
    ):
        outcome = review_for_kind(FailureKind.AUTH, evidence)
        assert outcome.enabled is True
        assert outcome.passed is False
        assert outcome.review_verified is False


def test_review_passes_when_score_meets_threshold(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1"])
    register_threshold(_schema_threshold(threshold=0.82))
    outcome = review_for_kind(FailureKind.SCHEMA, {"typed_review_score": 0.82})
    assert outcome.enabled is True
    assert outcome.passed is True
    assert outcome.review_verified is True
    assert outcome.kind == "schema"


# ── completion evidence helpers ────────────────────────────────────


def test_completion_evidence_only_when_verified():
    verified = TypedReviewOutcome(
        kind="auth",
        enabled=True,
        passed=True,
        reasons=["ok"],
        review_verified=True,
    )
    assert completion_evidence_from_outcome(verified) == {"review_verified": True}
    denied = TypedReviewOutcome(
        kind="auth",
        enabled=True,
        passed=False,
        reasons=["no"],
        review_verified=False,
    )
    assert completion_evidence_from_outcome(denied) == {}


def test_apply_typed_review_sets_review_verified_for_completion(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.UNKNOWN, ["u1"])
    register_threshold(_unknown_threshold())
    evidence = apply_typed_review(FailureKind.UNKNOWN, {"typed_review_score": 0.95})
    assert evidence == {"review_verified": True}
    conditions = [CompletionCondition("review_approved", {})]
    verdict = evaluate_completion_conditions(conditions, evidence)
    assert verdict.satisfied is True


def test_llm_approved_alone_still_fails_completion(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.TRANSIENT, ["t1"])
    register_threshold(_transient_threshold())
    # Typed path did not verify; LLM JSON approved must not satisfy.
    evidence = apply_typed_review(FailureKind.TRANSIENT, {"approved": True})
    assert evidence == {}
    conditions = [
        CompletionCondition("review_approved", {}),
        CompletionCondition("approved", {}),
    ]
    verdict = evaluate_completion_conditions(
        conditions,
        {"approved": True, "review": {"approved": True}},
    )
    assert verdict.satisfied is False


# ── parse_review unchanged ─────────────────────────────────────────


def test_parse_review_still_works_when_typed_path_off():
    review = parse_review('{"approved": true, "issues": [], "suggestions": []}')
    assert review is not None
    assert review.approved is True
    assert review.issues == []
    assert review.suggestions == []
    bad = parse_review("Looks good")
    assert bad is None


# ── no calibrated 0.5 default ──────────────────────────────────────


def test_module_has_no_calibrated_half_threshold():
    source = _MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # No module-level DEFAULT_*THRESHOLD* = … / calibrated mid-score constants.
    # Local names like ``threshold = artifact.threshold`` are fine (T10).
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id.upper()
                if "DEFAULT" in name and "THRESHOLD" in name:
                    pytest.fail(f"unexpected default threshold constant: {target.id}")
                if name.endswith("_THRESHOLD") and isinstance(node.value, ast.Constant):
                    pytest.fail(f"unexpected threshold constant: {target.id}")
        if isinstance(node, ast.Constant) and node.value == 0.5:
            pytest.fail("typed_review must not ship a calibrated 0.5 default")
    assert "DEFAULT_THRESHOLD" not in source
