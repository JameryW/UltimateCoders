"""Tests for PAIChecker cleaning and SWE-DIVE simulation (ledger #65)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.paichecker_swedive import (
    PAICHECKER_SWEDIVE_ENV,
    CleaningReport,
    DynamicUserSimulator,
    MisalignmentFinding,
    MisalignmentPattern,
    PAIChecker,
    PaicheckerError,
    SimulatorRole,
    TaskRecord,
    UserTurn,
    paichecker_swedive_enabled,
)

LONG_ISSUE = (
    "The cache invalidation path in src/cache.py loses entries and returns "
    "stale values; update invalidate and cover it in tests/test_cache.py."
)


def _task(**overrides: object) -> TaskRecord:
    payload = {
        "instance_id": "task-1",
        "issue": LONG_ISSUE,
        "patch_paths": ("src/cache.py",),
        "test_paths": ("tests/test_cache.py",),
        "changed_symbols": ("invalidate",),
        "issue_terms": ("invalidate",),
    }
    payload.update(overrides)
    return TaskRecord(**payload)  # type: ignore[arg-type]


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAICHECKER_SWEDIVE_ENV, raising=False)
    assert not paichecker_swedive_enabled()
    monkeypatch.setenv(PAICHECKER_SWEDIVE_ENV, "on")
    assert paichecker_swedive_enabled()


def test_detect_flags_unrelated_module_and_tests() -> None:
    task = _task(
        patch_paths=("src/other.py",),
        test_paths=("tests/test_other.py",),
        changed_symbols=("other",),
        issue_terms=("cache",),
    )
    patterns = {finding.pattern for finding in PAIChecker().detect(task)}
    assert MisalignmentPattern.UNRELATED_MODULE in patterns
    assert MisalignmentPattern.UNRELATED_TESTS in patterns


def test_detect_flags_test_only_and_short_issue() -> None:
    task = _task(
        issue="short",
        patch_paths=("tests/test_cache.py",),
        test_paths=("tests/test_cache.py",),
    )
    patterns = {finding.pattern for finding in PAIChecker().detect(task)}
    assert MisalignmentPattern.TEST_ONLY_CHANGE in patterns
    assert MisalignmentPattern.UNDER_SPECIFIED_ISSUE in patterns


def test_aligned_task_is_accepted() -> None:
    report = PAIChecker().clean((_task(),))
    assert report.accepted == 1
    assert report.rejected == 0
    assert report.decisions[0].accepted


def test_test_only_change_is_rejected_by_code_verify() -> None:
    task = _task(
        issue=LONG_ISSUE + " Only tests are changed.",
        patch_paths=("tests/test_cache.py",),
        test_paths=("tests/test_cache.py",),
        changed_symbols=(),
    )
    report = PAIChecker().clean((task,))
    assert report.decisions[0].accepted is False
    assert "tests" in report.decisions[0].reason


def test_multi_file_patch_for_unrelated_module_is_rejected() -> None:
    task = _task(
        patch_paths=("src/a.py", "src/b.py", "src/c.py", "src/d.py"),
        changed_symbols=(),
        issue_terms=(),
    )
    report = PAIChecker().clean((task,))
    assert report.decisions[0].accepted is False
    assert "multi-file" in report.decisions[0].reason


def test_under_specified_issue_cannot_justify_multi_file_patch() -> None:
    task = _task(
        issue="short",
        patch_paths=("src/cache.py", "src/other.py"),
        changed_symbols=(),
        issue_terms=("invalidate",),
    )
    report = PAIChecker().clean((task,))
    assert report.decisions[0].accepted is False
    assert "multi-file" in report.decisions[0].reason


def test_synthesize_prefers_strongest_and_honours_majority() -> None:
    checker = PAIChecker()
    findings = (
        MisalignmentFinding(MisalignmentPattern.UNRELATED_MODULE, 0.6, "a"),
        MisalignmentFinding(MisalignmentPattern.TEST_ONLY_CHANGE, 0.9, "b"),
    )
    assert checker.synthesize(findings) is MisalignmentPattern.TEST_ONLY_CHANGE
    assert (
        checker.synthesize(
            findings,
            cross_agent_votes=(
                MisalignmentPattern.UNRELATED_MODULE,
                MisalignmentPattern.UNRELATED_MODULE,
            ),
        )
        is MisalignmentPattern.UNRELATED_MODULE
    )
    assert checker.synthesize(()) is None


def test_clean_report_counts_and_rejects_empty_batch() -> None:
    report = PAIChecker().clean((_task(), _task(instance_id="task-2", issue="short")))
    assert isinstance(report, CleaningReport)
    assert report.accepted + report.rejected == 2
    with pytest.raises(PaicheckerError):
        PAIChecker().clean(())


def test_task_record_validation_and_test_only_property() -> None:
    assert _task(patch_paths=("tests/a.py",)).test_only
    assert not _task().test_only
    with pytest.raises(PaicheckerError):
        _task(instance_id="")
    with pytest.raises(PaicheckerError):
        _task(patch_paths=("src/a.py", "src/a.py"))


def test_user_simulator_turns_and_validation() -> None:
    simulator = DynamicUserSimulator("Add caching to the handler.")
    first = simulator.turn(1)
    second = simulator.turn(2)
    third = simulator.turn(3)
    assert first is not None and first.role is SimulatorRole.MIND_CHANGER
    assert second is not None and "old api" in second.required_terms
    assert third is not None and third.role is SimulatorRole.EXPERT_NITPICKER
    assert simulator.turn(4) is None
    assert simulator.validate(second, "keep the old API and add the filter").valid
    bad = simulator.validate(second, "remove the old API")
    assert not bad.valid
    assert bad.forbidden_terms == ("remove the old api",)
    with pytest.raises(PaicheckerError):
        DynamicUserSimulator("requirement", max_turns=9)


def test_session_run_pass_and_fail() -> None:
    simulator = DynamicUserSimulator("Add caching.")
    passed = simulator.run(
        (
            "I will add caching.",
            "keep the old API and add the filter",
            "removed the dead branch and added a regression test",
        )
    )
    assert passed.passed
    failed = simulator.run(("I will add caching.", "remove the old API"))
    assert not failed.passed
    assert failed.validations[1].forbidden_terms == ("remove the old api",)


def test_user_turn_rejects_empty_prompt() -> None:
    with pytest.raises(PaicheckerError):
        UserTurn(turn=1, role=SimulatorRole.MIND_CHANGER, prompt="")
