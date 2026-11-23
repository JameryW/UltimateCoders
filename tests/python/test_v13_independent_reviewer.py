"""Tests for the v13 independent L3 reviewer (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.independent_reviewer import (
    INDEPENDENT_REVIEWER_ENABLED_ENV,
    BoundaryView,
    IndependentReviewer,
    ReviewDecision,
    TestReport,
    changed_paths,
    independent_reviewer_enabled,
)

PATCH = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)

TEST_PATCH = (
    "diff --git a/tests/test_a.py b/tests/test_a.py\n"
    "--- a/tests/test_a.py\n"
    "+++ b/tests/test_a.py\n"
    "@@ -1 +1 @@\n"
    "-assert value == 1\n"
    "+assert value == 2\n"
)


def report() -> TestReport:
    return TestReport(passed=3, failed=0, total=3, summary="ok")


def boundary() -> BoundaryView:
    return BoundaryView(
        required_paths=("src/a.py",),
        protected_paths=("tests/test_a.py",),
        editable_paths=("src/a.py",),
    )


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INDEPENDENT_REVIEWER_ENABLED_ENV, raising=False)
    assert independent_reviewer_enabled() is False
    monkeypatch.setenv(INDEPENDENT_REVIEWER_ENABLED_ENV, "yes")
    assert independent_reviewer_enabled() is True


def test_clean_patch_is_approved() -> None:
    verdict = IndependentReviewer().review(
        PATCH, test_report=report(), boundary=boundary()
    )
    assert verdict.decision is ReviewDecision.APPROVE
    assert verdict.approved is True
    assert verdict.isolation == "L3"
    assert verdict.test_evidence is True
    assert verdict.findings == ()


def test_missing_test_evidence_is_rejected() -> None:
    verdict = IndependentReviewer().review(PATCH, test_report=None)
    assert verdict.decision is ReviewDecision.REJECT
    assert {finding.code for finding in verdict.findings} == {"no_test_evidence"}


def test_failed_tests_are_rejected() -> None:
    verdict = IndependentReviewer().review(
        PATCH, test_report=TestReport(passed=1, failed=2, total=3)
    )
    assert verdict.decision is ReviewDecision.REJECT
    assert any(finding.code == "tests_failed" for finding in verdict.findings)


def test_zero_execution_is_rejected() -> None:
    verdict = IndependentReviewer().review(
        PATCH, test_report=TestReport(passed=0, failed=0, total=0)
    )
    assert verdict.decision is ReviewDecision.REJECT
    assert any(finding.code == "tests_not_executed" for finding in verdict.findings)


def test_test_file_modification_is_rejected_and_path_is_reported() -> None:
    verdict = IndependentReviewer().review(TEST_PATCH, test_report=report())
    assert verdict.decision is ReviewDecision.REJECT
    assert any(finding.code == "test_file_modified" for finding in verdict.findings)
    assert "tests/test_a.py" in verdict.reviewed_paths


def test_assertion_deletion_is_rejected() -> None:
    verdict = IndependentReviewer().review(TEST_PATCH, test_report=report())
    assert any(finding.code == "assertion_deleted" for finding in verdict.findings)


def test_verification_bypass_markers_are_rejected() -> None:
    patched = PATCH.replace("+new", '+pytest.skip("later")')
    verdict = IndependentReviewer().review(patched, test_report=report())
    assert verdict.decision is ReviewDecision.REJECT
    assert any(finding.code == "verification_bypass" for finding in verdict.findings)


def test_protected_paths_are_rejected() -> None:
    verdict = IndependentReviewer().review(
        TEST_PATCH, test_report=report(), boundary=boundary()
    )
    assert any(finding.code == "protected_path_modified" for finding in verdict.findings)


def test_missing_required_path_requests_changes() -> None:
    other = PATCH.replace("src/a.py", "src/b.py")
    verdict = IndependentReviewer().review(
        other, test_report=report(), boundary=boundary()
    )
    assert verdict.decision is ReviewDecision.NEEDS_CHANGES
    assert any(finding.code == "required_path_missing" for finding in verdict.findings)


def test_empty_patch_is_rejected() -> None:
    verdict = IndependentReviewer().review("", test_report=report())
    assert verdict.decision is ReviewDecision.REJECT
    assert any(finding.code == "empty_patch" for finding in verdict.findings)


def test_review_many_is_bounded_by_max_findings() -> None:
    reviewer = IndependentReviewer(max_findings=1)
    verdict = reviewer.review(TEST_PATCH, test_report=report())
    assert len(verdict.findings) == 1


def test_brief_contains_no_trajectory_and_is_bounded() -> None:
    reviewer = IndependentReviewer()
    brief = reviewer.build_brief(
        PATCH, test_report=report(), boundary=boundary(), max_chars=120
    )
    assert "L3 isolated review" in brief
    assert "trajectory" not in brief.lower()
    assert len(brief) <= 120


def test_changed_paths_normalises_git_prefixes() -> None:
    assert changed_paths(PATCH) == ("src/a.py",)
    assert changed_paths("+++ /dev/null\n") == ()


def test_invalid_test_report_is_rejected() -> None:
    with pytest.raises(ValueError):
        TestReport(passed=2, failed=2, total=3)
    with pytest.raises(ValueError):
        TestReport(passed=-1, failed=0, total=1)
