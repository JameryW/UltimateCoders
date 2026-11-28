"""Tests for the v14 Foresight-SWE residual and anticipation gate (default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.foresight_residual_auditor import (
    FORESIGHT_RESIDUAL_AUDITOR_ENV,
    UNVERIFIED_RESIDUAL_TAINT,
    BoundaryKind,
    ForesightAuditor,
    ForesightResidualAuditor,
    ForesightSynthesizer,
    ResidualAuditError,
    ResidualFile,
    ResidualWorkspace,
    foresight_residual_auditor_enabled,
)


def workspace(*files: ResidualFile) -> ResidualWorkspace:
    return ResidualWorkspace(attempt_id="attempt-3", files=files, base_commit="abc123")


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FORESIGHT_RESIDUAL_AUDITOR_ENV, raising=False)
    assert foresight_residual_auditor_enabled() is False
    monkeypatch.setenv(FORESIGHT_RESIDUAL_AUDITOR_ENV, "true")
    assert foresight_residual_auditor_enabled() is True


def test_clean_workspace_has_no_taint() -> None:
    residual = workspace(
        ResidualFile("src/a.py", "clean", dirty=False, lines_changed=0),
        ResidualFile("src/b.py", "clean", dirty=False, lines_changed=0),
    )
    audit = ForesightAuditor().audit(residual, issue_terms=("cache",))
    assert audit.clean is True
    assert audit.tainted == ()
    assert audit.requires_reanchor is False
    assert audit.dirty_ratio == 0.0


def test_dirty_files_receive_the_residual_taint_label() -> None:
    residual = workspace(
        ResidualFile("src/cache.py", "def purge(): ...", dirty=True, lines_changed=12),
        ResidualFile("src/clean.py", "clean", dirty=False),
    )
    audit = ForesightAuditor().audit(residual, issue_terms=("cache invalidation",))
    assert audit.clean is False
    assert len(audit.tainted) == 1
    assert audit.tainted[0].label == UNVERIFIED_RESIDUAL_TAINT
    assert audit.tainted[0].path == "src/cache.py"
    assert audit.tainted[0].lines_changed == 12
    assert audit.requires_reanchor is True
    assert audit.dirty_ratio == pytest.approx(0.5)


def test_issue_overlap_is_reported_and_gap_is_kept() -> None:
    residual = workspace(
        ResidualFile(
            "src/cache.py",
            "cache invalidation uses Store.purge()",
            dirty=True,
        ),
    )
    audit = ForesightAuditor().audit(
        residual,
        issue_terms=("cache invalidation", "tenant isolation"),
    )
    assert audit.issue_overlap_terms == ("cache invalidation",)
    assert audit.coverage_gap == ("tenant isolation",)


def test_invalid_auditor_thresholds_are_rejected() -> None:
    with pytest.raises(ResidualAuditError):
        ForesightAuditor(taint_threshold=1.1)
    with pytest.raises(ResidualAuditError):
        ForesightAuditor(overlap_threshold=0)


def test_anticipation_boundaries_are_classified_deterministically() -> None:
    synthesizer = ForesightSynthesizer()
    tests = synthesizer.synthesize(
        ("parse_config", "lock_writer", "count_items", "handle_request"),
        issue_terms=("cache",),
    )
    assert [test.kind for test in tests] == [
        BoundaryKind.NULL,
        BoundaryKind.CONCURRENCY,
        BoundaryKind.TYPE_OVERFLOW,
        BoundaryKind.CONTRACT_COMPATIBILITY,
    ]
    assert all(test.target for test in tests)
    assert "issue terms" in tests[0].description


def test_combined_report_is_foresight_ready_when_tests_exist() -> None:
    residual = workspace(ResidualFile("src/cache.py", "dirty", dirty=True))
    report = ForesightResidualAuditor().evaluate(
        residual,
        changed_functions=("invalidate_cache",),
        issue_terms=("cache",),
    )
    assert report.foresight_ready is True
    assert len(report.anticipation) == 1
    data = report.to_dict()
    assert data["residual"]["requires_reanchor"] is True  # type: ignore[index]
    assert data["foresight_ready"] is True


def test_empty_function_list_is_not_foresight_ready() -> None:
    residual = workspace()
    report = ForesightResidualAuditor().evaluate(residual, changed_functions=())
    assert report.anticipation == ()
    assert report.foresight_ready is False


def test_empty_function_name_is_rejected() -> None:
    with pytest.raises(ResidualAuditError):
        ForesightSynthesizer().synthesize(("",))
