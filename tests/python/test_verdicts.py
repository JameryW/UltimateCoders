"""Verification extraction must report only what a backend actually said.

Principle A: a missing or ambiguous verdict stays unknown. These tests pin the
empty-value contract (never a fabricated pass/fail), the mapping/object
tolerance, and the bounded compact label stored on an attempt round.
"""

from __future__ import annotations

from types import SimpleNamespace

from ultimate_coders.agent.types import SubtaskReview
from ultimate_coders.knowledge.verdicts import (
    MAX_ISSUES,
    MAX_REASONS,
    REVIEW_APPROVED,
    REVIEW_REJECTED,
    VerificationReport,
    extract_verification,
)


class TestEmptyContract:
    def test_nothing_reported_is_unknown_not_failed(self):
        report = extract_verification()

        assert report == VerificationReport()
        assert report.has_any() is False
        assert report.round_verdict() == "unknown"

    def test_empty_domain_result_is_unknown(self):
        assert extract_verification(domain_result={}).round_verdict() == "unknown"

    def test_unrecognised_shapes_are_ignored_not_guessed(self):
        report = extract_verification(
            review=SimpleNamespace(comment="LGTM"),
            domain_result={"oracle": {"score": 0.9}, "tests": {"count": 3}},
        )

        assert report == VerificationReport()

    def test_string_review_without_a_boolean_is_ignored(self):
        assert extract_verification(review="approved").review_verdict == ""


class TestReview:
    def test_dataclass_review_carries_approval_and_issues(self):
        report = extract_verification(
            review=SubtaskReview(approved=True, issues=["missing test", "naming"])
        )

        assert report.review_verdict == REVIEW_APPROVED
        assert report.review_issues == ("missing test", "naming")
        assert report.round_verdict() == "review=approved"

    def test_mapping_review_uses_the_same_fields(self):
        report = extract_verification(review={"approved": False, "issues": ["bad"]})

        assert report.review_verdict == REVIEW_REJECTED
        assert report.review_issues == ("bad",)

    def test_alternative_review_keys_are_accepted(self):
        assert extract_verification(review={"success": True}).review_verdict == REVIEW_APPROVED
        assert extract_verification(review={"accepted": "no"}).review_verdict == REVIEW_REJECTED

    def test_issues_are_bounded(self):
        issues = [f"issue {index}" for index in range(MAX_ISSUES + 3)]

        report = extract_verification(review={"approved": True, "issues": issues})

        assert len(report.review_issues) == MAX_ISSUES
        assert report.review_issues[0] == "issue 0"


class TestOracle:
    def test_accepted_boolean_and_reasons(self):
        report = extract_verification(
            domain_result={"oracle": {"accepted": True, "reasons": ["faster", "smaller"]}}
        )

        assert report.oracle_verdict == "accepted"
        assert report.oracle_reasons == ("faster", "smaller")
        assert report.round_verdict() == "oracle=accepted"

    def test_word_verdict_is_accepted_when_no_boolean_exists(self):
        assert (
            extract_verification(domain_result={"oracle": {"verdict": "PASS"}}).oracle_verdict
            == "accepted"
        )
        assert (
            extract_verification(domain_result={"oracle": {"result": "fail"}}).oracle_verdict
            == "rejected"
        )

    def test_reasons_are_bounded(self):
        reasons = [f"reason {index}" for index in range(MAX_REASONS + 4)]

        report = extract_verification(
            domain_result={"oracle": {"accepted": False, "reasons": reasons}}
        )

        assert report.oracle_verdict == "rejected"
        assert len(report.oracle_reasons) == MAX_REASONS


class TestTests:
    def test_explicit_test_verdict_string(self):
        assert (
            extract_verification(domain_result={"test_verdict": "passed"}).test_verdict
            == "passed"
        )
        assert (
            extract_verification(domain_result={"tests_verdict": "FAILED"}).test_verdict
            == "failed"
        )

    def test_tests_block_with_boolean_and_summary(self):
        report = extract_verification(
            domain_result={"tests": {"passed": True, "summary": "12 passed in 3s"}}
        )

        assert report.test_verdict == "passed"
        assert report.test_summary == "12 passed in 3s"
        assert report.round_verdict() == "test=passed"

    def test_falling_back_to_verification_block(self):
        report = extract_verification(domain_result={"verification": {"ok": False}})

        assert report.test_verdict == "failed"

    def test_verification_passed_flag_is_the_last_resort(self):
        assert (
            extract_verification(domain_result={"verification_passed": True}).test_verdict
            == "passed"
        )

    def test_ambiguous_summary_does_not_become_a_verdict(self):
        assert (
            extract_verification(domain_result={"tests": {"summary": "1 failing"}}).test_verdict
            == ""
        )


class TestCombinedLabel:
    def test_all_three_verdicts_are_ordered(self):
        report = extract_verification(
            review={"approved": True},
            domain_result={"oracle": {"accepted": True}, "tests": {"passed": True}},
        )

        assert report.has_any() is True
        assert report.round_verdict() == "review=approved oracle=accepted test=passed"

    def test_mixed_verdicts_keep_only_reported_parts(self):
        report = extract_verification(domain_result={"tests": {"passed": False}})

        assert report.round_verdict() == "test=failed"
