"""Label corpus manifests (P5 / D12).

The repository holds provenance, not labels: a manifest names the kind, the human
labeler, the corpus URI, the count, and the eval roles the labelled attempts came
from. Report-role labels are refused (hold-out leakage), agent/worker/service
labelers are refused, and a CV artifact can be checked against the manifest that
backs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ultimate_coders.agent.review_labels import SCHEMA_VERSION, load_label_manifest
from ultimate_coders.agent.typed_review import (
    REVIEWER_LABEL_PROVENANCE_ENV,
    clear_thresholds,
    load_threshold_artifact,
    register_threshold,
    threshold_available,
    threshold_provenance,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reviewer_cv_threshold_schema.json"


def _manifest(**overrides) -> dict:
    data = {
        "schema_version": SCHEMA_VERSION,
        "kind": "schema",
        "labeler": "user:alice",
        "labeled_at": "2026-11-09T00:00:00Z",
        "n_labels": 40,
        "source_eval_roles": ["search", "select"],
        "corpus_uri": "s3://uc-eval/labels/schema.jsonl",
    }
    data.update(overrides)
    return data


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_artifact(tmp_path: Path, **overrides) -> Path:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    data.update(overrides)
    path = tmp_path / "threshold.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_valid_manifest_loads(tmp_path):
    manifest = load_label_manifest(_write_manifest(tmp_path, _manifest()))
    assert manifest.kind == "schema"
    assert manifest.labeler == "user:alice"
    assert manifest.n_labels == 40
    assert manifest.source_eval_roles == ("search", "select")
    assert manifest.corpus_uri == "s3://uc-eval/labels/schema.jsonl"


def test_a_search_only_manifest_loads(tmp_path):
    manifest = load_label_manifest(
        _write_manifest(tmp_path, _manifest(source_eval_roles=["search"]))
    )
    assert manifest.source_eval_roles == ("search",)


def test_missing_file_refuses(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_label_manifest(tmp_path / "absent.json")


def test_corrupt_json_refuses(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt JSON"):
        load_label_manifest(path)


def test_a_missing_field_refuses(tmp_path):
    data = _manifest()
    del data["corpus_uri"]
    with pytest.raises(ValueError, match="missing"):
        load_label_manifest(_write_manifest(tmp_path, data))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"kind": "holdout"}, "FailureKind"),
        ({"labeler": "agent:coder"}, "human principal"),
        ({"labeler": "worker:w1"}, "human principal"),
        ({"labeler": "service:resizer"}, "human principal"),
        ({"labeler": ""}, "labeler"),
        ({"labeled_at": ""}, "labeled_at"),
        ({"n_labels": 0}, "n_labels"),
        ({"n_labels": True}, "n_labels"),
        ({"source_eval_roles": []}, "source_eval_roles"),
        ({"source_eval_roles": ["report"]}, "report hold-out"),
        ({"source_eval_roles": ["holdout"]}, "not one of"),
        ({"corpus_uri": "labels/schema.jsonl"}, "scheme"),
    ],
)
def test_invalid_manifests_refuse(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_label_manifest(_write_manifest(tmp_path, _manifest(**overrides)))


# ── artifact <-> manifest cross-checks ───────────────────────────


def test_an_artifact_with_its_manifest_loads(tmp_path):
    manifest = load_label_manifest(_write_manifest(tmp_path, _manifest()))
    artifact = load_threshold_artifact(_FIXTURE, label_manifest=manifest)
    assert artifact.n_labels == 40
    assert artifact.cv_folds == 5


def test_artifact_kind_must_match_the_manifest(tmp_path):
    manifest = load_label_manifest(_write_manifest(tmp_path, _manifest(kind="transient")))
    with pytest.raises(ValueError, match="does not match"):
        load_threshold_artifact(_FIXTURE, label_manifest=manifest)


def test_artifact_count_must_match_the_manifest(tmp_path):
    manifest = load_label_manifest(_write_manifest(tmp_path, _manifest(n_labels=41)))
    with pytest.raises(ValueError, match="manifest holds 41"):
        load_threshold_artifact(_FIXTURE, label_manifest=manifest)


def test_artifact_below_the_structural_floor_is_refused(tmp_path):
    # cv_folds=5 in the fixture, so the floor is 10 labels; 9 cannot fill the folds.
    artifact_path = _write_artifact(tmp_path, n_labels=9)
    manifest = load_label_manifest(_write_manifest(tmp_path, _manifest(n_labels=9)))
    with pytest.raises(ValueError, match="structural floor"):
        load_threshold_artifact(artifact_path, label_manifest=manifest)


def test_artifact_without_a_manifest_keeps_the_old_check(tmp_path):
    artifact = load_threshold_artifact(_write_artifact(tmp_path, n_labels=9))
    assert artifact.n_labels == 9, "no manifest means no corpus claim to check"


# ── registration provenance (T28 / D12) ─────────────────────────


def test_registration_without_a_manifest_is_historical_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv(REVIEWER_LABEL_PROVENANCE_ENV, raising=False)
    clear_thresholds()
    try:
        register_threshold(load_threshold_artifact(_FIXTURE))
        assert threshold_available("schema")
        assert threshold_provenance("schema") == "unverified"
    finally:
        clear_thresholds()


def test_registration_requires_a_manifest_when_the_flag_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv(REVIEWER_LABEL_PROVENANCE_ENV, "1")
    clear_thresholds()
    try:
        with pytest.raises(ValueError, match="label manifest required"):
            register_threshold(load_threshold_artifact(_FIXTURE))
        assert threshold_provenance("schema") is None
        assert not threshold_available("schema")
    finally:
        clear_thresholds()


def test_registration_with_a_matching_manifest_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv(REVIEWER_LABEL_PROVENANCE_ENV, "1")
    clear_thresholds()
    try:
        manifest = load_label_manifest(_write_manifest(tmp_path, _manifest()))
        register_threshold(load_threshold_artifact(_FIXTURE), label_manifest=manifest)
        assert threshold_available("schema")
        assert threshold_provenance("schema") == "manifest"
    finally:
        clear_thresholds()


def test_registration_refuses_a_manifest_that_does_not_back_the_artifact(tmp_path, monkeypatch):
    clear_thresholds()
    try:
        manifest = load_label_manifest(_write_manifest(tmp_path, _manifest(n_labels=41)))
        with pytest.raises(ValueError, match="manifest holds 41"):
            register_threshold(load_threshold_artifact(_FIXTURE), label_manifest=manifest)
        assert threshold_provenance("schema") is None
    finally:
        clear_thresholds()
