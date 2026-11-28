from __future__ import annotations

import pytest
from ultimate_coders.acceptance.swe_whatif_replay import (
    SWE_WHATIF_REPLAY_ENV,
    CounterfactualReplay,
    OrganizationRole,
    OrganizationTask,
    RecordedSession,
    SessionTurn,
    SweWhatIfError,
    UserPreferenceChange,
    evaluate_organization,
    extract_pushback_points,
    swe_whatif_replay_enabled,
)


def session() -> RecordedSession:
    return RecordedSession(
        "s1",
        (
            SessionTurn(0, "user", "please add caching"),
            SessionTurn(1, "assistant", "I will edit cache.py without reading callers"),
            SessionTurn(2, "user", "you missed the invalidation path", pushback=True),
        ),
    )


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWE_WHATIF_REPLAY_ENV, "on")
    assert swe_whatif_replay_enabled()
    monkeypatch.setenv(SWE_WHATIF_REPLAY_ENV, "")
    assert not swe_whatif_replay_enabled()


def test_extract_pushback_points_finds_previous_action() -> None:
    points = extract_pushback_points(session())
    assert len(points) == 1
    assert points[0].turn_index == 2
    assert points[0].previous_action.startswith("I will edit")


def test_replay_marks_changed_action_as_avoided() -> None:
    report = CounterfactualReplay(lambda point: "read callers and invalidation path first").replay(
        session()
    )
    assert report.avoidance_rate == 1.0
    assert report.results[0].avoided


def test_replay_marks_repeated_action_as_not_avoided() -> None:
    report = CounterfactualReplay(lambda point: point.previous_action).replay(session())
    assert report.avoidance_rate == 0.0


def test_replay_without_pushbacks_has_zero_rate() -> None:
    clean = RecordedSession("clean", (SessionTurn(0, "user", "hello"),))
    report = CounterfactualReplay().replay(clean)
    assert report.avoidance_rate == 0.0
    assert report.results == ()


def test_report_to_dict_is_bounded() -> None:
    report = CounterfactualReplay(lambda point: "different").replay(session())
    payload = report.to_dict()
    assert payload["session_id"] == "s1"
    assert payload["avoidance_rate"] == 1.0
    assert isinstance(payload["results"], list)


def test_preference_change_detects_difference() -> None:
    assert UserPreferenceChange("A", "B").changed
    assert not UserPreferenceChange("A", "A").changed


def test_organization_task_reports_missing_roles() -> None:
    task = OrganizationTask("t1", ("planner", "coder", "reviewer"))
    roles = [OrganizationRole("planner", "plan"), OrganizationRole("coder", "code")]
    report = evaluate_organization(task, roles)
    assert report.coverage == pytest.approx(2 / 3)
    assert report.missing_roles == ("reviewer",)
    assert not report.complete


def test_organization_task_rejects_missing_roles_or_ids() -> None:
    with pytest.raises(SweWhatIfError):
        OrganizationTask("", ("planner",))
    with pytest.raises(SweWhatIfError):
        OrganizationTask("t1", ())


def test_invalid_session_shapes_are_rejected() -> None:
    with pytest.raises(SweWhatIfError):
        RecordedSession("", (SessionTurn(0, "user", "x"),))
    with pytest.raises(SweWhatIfError):
        RecordedSession("s", (SessionTurn(1, "user", "x"), SessionTurn(0, "user", "y")))
    with pytest.raises(SweWhatIfError):
        SessionTurn(0, "narrator", "x")
    with pytest.raises(SweWhatIfError):
        SessionTurn(-1, "user", "x")
