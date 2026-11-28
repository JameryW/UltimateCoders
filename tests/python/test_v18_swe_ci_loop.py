"""Tests for the V18 SWE-CI loop (#72)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.swe_ci_loop import (
    ArchitectAgent,
    CiCommit,
    ProgrammerAgent,
    SweCiLoop,
    swe_ci_loop_enabled,
)


def commit(index: int, tests: set[str], **kwargs) -> CiCommit:
    return CiCommit(index=index, passed_tests=frozenset(tests), **kwargs)


def test_flag_defaults_off_and_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not swe_ci_loop_enabled()
    monkeypatch.setenv("UC_SWE_CI_LOOP", "1")
    assert swe_ci_loop_enabled()


def test_monotone_commits_are_accepted() -> None:
    report = SweCiLoop().run(
        (
            commit(1, {"a"}),
            commit(2, {"a", "b"}),
            commit(3, {"a", "b", "c"}),
        )
    )
    assert report.accepted == 3
    assert report.final_pass_set == frozenset({"a", "b", "c"})
    assert report.zero_regression_rate == 1.0


def test_regression_is_rejected_and_pass_set_held() -> None:
    report = SweCiLoop().run(
        (
            commit(1, {"a", "b"}),
            commit(2, {"a"}),
            commit(3, {"a", "b", "c"}),
        )
    )
    assert report.decisions[1].accepted is False
    assert "regressed tests: b" in report.decisions[1].reason
    assert report.final_pass_set == frozenset({"a", "b", "c"})
    assert report.accepted == 2


def test_architecture_invariant_is_rejected() -> None:
    report = SweCiLoop().run(
        (commit(1, {"a"}, invariants_preserved=False),)
    )
    assert report.decisions[0].accepted is False
    assert "invariant" in report.decisions[0].reason


def test_coupling_ceiling_is_enforced() -> None:
    loop = SweCiLoop(architect=ArchitectAgent(max_coupling=0.5))
    report = loop.run((commit(1, {"a"}, coupling=0.8),))
    assert report.decisions[0].accepted is False
    assert "coupling" in report.decisions[0].reason


def test_zero_regression_rate_counts_rejections() -> None:
    report = SweCiLoop().run(
        (
            commit(1, {"a"}),
            commit(2, set()),
        )
    )
    assert report.accepted == 1
    assert report.rejected == 1
    assert report.zero_regression_rate == 0.5


def test_loop_can_recover_after_a_rejected_commit() -> None:
    report = SweCiLoop().run(
        (
            commit(1, {"a"}),
            commit(2, set()),
            commit(3, {"a", "b"}),
        )
    )
    assert [decision.accepted for decision in report.decisions] == [True, False, True]
    assert report.final_pass_set == frozenset({"a", "b"})


def test_programmer_agent_is_seedable() -> None:
    assert ProgrammerAgent().prepare(commit(1, {"a"})).passed_tests == frozenset({"a"})


def test_invalid_commit_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="index"):
        CiCommit(index=0, passed_tests=frozenset())
    with pytest.raises(ValueError, match="coupling"):
        CiCommit(index=1, passed_tests=frozenset(), coupling=1.2)
    with pytest.raises(ValueError, match="max_coupling"):
        ArchitectAgent(max_coupling=2.0)


def test_report_serialises_decisions() -> None:
    report = SweCiLoop().run((commit(1, {"a"}), commit(2, set())))
    data = report.to_dict()
    assert data["accepted"] == 1
    assert data["rejected"] == 1
    assert data["final_pass_set"] == ["a"]
    assert data["decisions"][1]["accepted"] is False
