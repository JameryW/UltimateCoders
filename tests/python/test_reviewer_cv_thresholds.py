"""Reviewer CV threshold artifacts (ticket 10 / D5).

Load success; missing/corrupt refuse; kind without artifact disabled;
score >= threshold passes; enforce off still inert; no hardcoded calibrated
0.5 default in product code.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest
from ultimate_coders.agent.completion_conditions import (
    CompletionCondition,
    evaluate_completion_conditions,
)
from ultimate_coders.agent.routing import FailureKind
from ultimate_coders.agent.typed_review import (
    TYPED_REVIEW_ENFORCE_ENV,
    ThresholdArtifact,
    apply_typed_review,
    clear_labels,
    clear_thresholds,
    load_threshold_artifact,
    register_labels,
    register_threshold,
    review_for_kind,
    threshold_available,
    threshold_for,
    typed_review_enforce_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reviewer_cv_threshold_schema.json"
_THRESHOLDS_MODULE = _REPO / "python" / "ultimate_coders" / "agent" / "typed_review_thresholds.py"
_TYPED_REVIEW_MODULE = _REPO / "python" / "ultimate_coders" / "agent" / "typed_review.py"


@pytest.fixture(autouse=True)
def _reset_typed_review_state(monkeypatch):
    clear_labels()
    clear_thresholds()
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    yield
    clear_labels()
    clear_thresholds()


def _register_schema_fixture() -> ThresholdArtifact:
    artifact = load_threshold_artifact(_FIXTURE)
    register_threshold(artifact)
    return artifact


# ── loader ─────────────────────────────────────────────────────────


def test_load_threshold_artifact_success():
    artifact = load_threshold_artifact(_FIXTURE)
    assert isinstance(artifact, ThresholdArtifact)
    assert artifact.kind == "schema"
    assert artifact.metric == "f1"
    assert artifact.threshold == pytest.approx(0.82)
    assert artifact.cv_folds == 5
    assert artifact.n_labels == 40
    assert artifact.generated_at == "2026-10-25T00:00:00Z"


def test_load_missing_artifact_refuses(tmp_path):
    missing = tmp_path / "absent.json"
    with pytest.raises(ValueError, match="not found"):
        load_threshold_artifact(missing)


def test_load_corrupt_json_refuses(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt JSON"):
        load_threshold_artifact(path)


@pytest.mark.parametrize(
    "payload,match",
    [
        ({}, "missing keys"),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 0.82,
                "cv_folds": 5,
                "n_labels": 40,
                # generated_at omitted
            },
            "missing keys",
        ),
        (
            {
                "kind": "not-a-kind",
                "metric": "f1",
                "threshold": 0.82,
                "cv_folds": 5,
                "n_labels": 40,
                "generated_at": "t",
            },
            "not a FailureKind",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": float("nan"),
                "cv_folds": 5,
                "n_labels": 40,
                "generated_at": "t",
            },
            "finite",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 0.0,
                "cv_folds": 5,
                "n_labels": 40,
                "generated_at": "t",
            },
            r"\(0, 1\]",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 1.5,
                "cv_folds": 5,
                "n_labels": 40,
                "generated_at": "t",
            },
            r"\(0, 1\]",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 0.82,
                "cv_folds": 1,
                "n_labels": 40,
                "generated_at": "t",
            },
            "cv_folds",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 0.82,
                "cv_folds": 5,
                "n_labels": 0,
                "generated_at": "t",
            },
            "n_labels",
        ),
        (
            {
                "kind": "schema",
                "metric": "f1",
                "threshold": 0.82,
                "cv_folds": 5,
                "n_labels": 40,
                "generated_at": "t",
                "calibrated_default": True,
            },
            "calibrated_default",
        ),
    ],
)
def test_load_invalid_artifact_refuses(tmp_path, payload, match):
    path = tmp_path / "invalid.json"
    # json.dumps turns nan into NaN token; write manually for nan case.
    if isinstance(payload.get("threshold"), float) and math.isnan(payload["threshold"]):
        path.write_text(
            '{"kind":"schema","metric":"f1","threshold":NaN,'
            '"cv_folds":5,"n_labels":40,"generated_at":"t"}',
            encoding="utf-8",
        )
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_threshold_artifact(path)


def test_measured_half_threshold_from_real_artifact_is_allowed(tmp_path):
    """A CV-measured 0.5 is legitimate; only calibrated_default is forbidden."""
    path = tmp_path / "half.json"
    path.write_text(
        json.dumps(
            {
                "kind": "auth",
                "metric": "f1",
                "threshold": 0.5,
                "cv_folds": 3,
                "n_labels": 20,
                "generated_at": "2026-10-25T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    artifact = load_threshold_artifact(path)
    assert artifact.threshold == pytest.approx(0.5)


# ── registry ───────────────────────────────────────────────────────


def test_threshold_registry_empty_by_default():
    for kind in FailureKind:
        assert threshold_available(kind.value) is False
        assert threshold_for(kind.value) is None


def test_register_threshold_makes_kind_available():
    artifact = _register_schema_fixture()
    assert threshold_available("schema") is True
    assert threshold_for("schema") == pytest.approx(0.82)
    assert threshold_available("auth") is False
    assert artifact.kind == "schema"


def test_clear_thresholds_resets_registry():
    _register_schema_fixture()
    clear_thresholds()
    assert threshold_available("schema") is False
    assert threshold_for("schema") is None


def test_register_threshold_normalizes_kind_and_refuses_unknown():
    register_threshold(
        ThresholdArtifact(
            kind="SCHEMA",
            metric="f1",
            threshold=0.8,
            cv_folds=3,
            n_labels=10,
            generated_at="t",
        )
    )
    assert threshold_available("schema") is True
    assert threshold_available("SCHEMA") is True
    assert threshold_for("Schema") == pytest.approx(0.8)
    with pytest.raises(ValueError, match="not a FailureKind"):
        register_threshold(
            ThresholdArtifact(
                kind="not-a-kind",
                metric="f1",
                threshold=0.8,
                cv_folds=3,
                n_labels=10,
                generated_at="t",
            )
        )
    with pytest.raises(TypeError, match="ThresholdArtifact"):
        register_threshold({"kind": "schema"})  # type: ignore[arg-type]


def test_load_refuses_non_object_and_wrong_field_types(tmp_path):
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_threshold_artifact(array_path)

    bad = {
        "kind": "schema",
        "metric": "f1",
        "threshold": "high",
        "cv_folds": 5,
        "n_labels": 40,
        "generated_at": "t",
    }
    path = tmp_path / "bad-types.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="threshold must be a number"):
        load_threshold_artifact(path)


# ── review_for_kind integration ────────────────────────────────────


def test_enforce_off_still_inert_with_labels_and_threshold(monkeypatch):
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    assert typed_review_enforce_enabled() is False
    register_labels(FailureKind.SCHEMA, ["s1"])
    _register_schema_fixture()
    outcome = review_for_kind(
        FailureKind.SCHEMA,
        {"typed_review_score": 0.99},
    )
    assert outcome.enabled is False
    assert outcome.passed is False
    assert outcome.review_verified is False
    assert TYPED_REVIEW_ENFORCE_ENV in outcome.reasons[0]


def test_kind_without_threshold_artifact_disabled(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1"])
    # No register_threshold → disabled even with labels + score.
    outcome = review_for_kind(FailureKind.SCHEMA, {"typed_review_score": 0.99})
    assert outcome.enabled is False
    assert outcome.passed is False
    assert outcome.review_verified is False
    assert "threshold artifact" in outcome.reasons[0]


def test_artifacts_alone_do_not_enable_without_enforce(monkeypatch):
    monkeypatch.delenv(TYPED_REVIEW_ENFORCE_ENV, raising=False)
    _register_schema_fixture()
    # No labels either; still inert.
    outcome = review_for_kind("schema", {"typed_review_score": 0.99})
    assert outcome.enabled is False
    assert outcome.review_verified is False


def test_score_at_or_above_threshold_passes(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1", "s2"])
    artifact = _register_schema_fixture()
    for score in (artifact.threshold, 0.82, 0.99, 1.0):
        outcome = review_for_kind(FailureKind.SCHEMA, {"typed_review_score": score})
        assert outcome.enabled is True
        assert outcome.passed is True
        assert outcome.review_verified is True


def test_score_below_threshold_fails_closed(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1"])
    _register_schema_fixture()
    outcome = review_for_kind(FailureKind.SCHEMA, {"typed_review_score": 0.81})
    assert outcome.enabled is True
    assert outcome.passed is False
    assert outcome.review_verified is False


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"typed_review_score": None},
        {"typed_review_score": "0.9"},
        {"typed_review_score": True},
        {"typed_review_score": float("nan")},
        {"typed_review_score": float("inf")},
        {"typed_review_pass": True},
        {"approved": True},
    ],
)
def test_missing_or_invalid_score_fails_closed(monkeypatch, evidence):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1"])
    _register_schema_fixture()
    outcome = review_for_kind(FailureKind.SCHEMA, evidence)
    assert outcome.enabled is True
    assert outcome.passed is False
    assert outcome.review_verified is False


def test_apply_typed_review_sets_review_verified_for_completion(monkeypatch):
    monkeypatch.setenv(TYPED_REVIEW_ENFORCE_ENV, "1")
    register_labels(FailureKind.SCHEMA, ["s1"])
    _register_schema_fixture()
    evidence = apply_typed_review(FailureKind.SCHEMA, {"typed_review_score": 0.9})
    assert evidence == {"review_verified": True}
    verdict = evaluate_completion_conditions(
        [CompletionCondition("review_approved", {})],
        evidence,
    )
    assert verdict.satisfied is True


# ── no hardcoded calibrated 0.5 default ────────────────────────────


def _assert_no_calibrated_half_default(module_path: Path) -> None:
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "THRESHOLD" in target.id.upper():
                    if isinstance(node.value, ast.Constant) and node.value == 0.5:
                        pytest.fail(f"calibrated 0.5 default: {target.id}")
                    if "DEFAULT" in target.id.upper():
                        pytest.fail(f"unexpected default threshold constant: {target.id}")
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id.upper()
            if "DEFAULT" in name and "THRESHOLD" in name:
                pytest.fail(f"unexpected default threshold annotation: {node.target.id}")
    assert "DEFAULT_THRESHOLD" not in source
    assert "calibrated_default" not in source or "must not set calibrated_default" in source


def test_modules_have_no_calibrated_half_threshold_default():
    _assert_no_calibrated_half_default(_THRESHOLDS_MODULE)
    _assert_no_calibrated_half_default(_TYPED_REVIEW_MODULE)
    # Fixture is a real CV artifact at 0.82, not a calibrated_default claim.
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture.get("calibrated_default") is not True
    assert fixture["threshold"] != 0.5 or fixture.get("n_labels", 0) >= 1
