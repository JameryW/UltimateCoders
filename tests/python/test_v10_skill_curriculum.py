"""Tests for the v10 skill cards and four-stage curriculum (#21)."""

from __future__ import annotations

from ultimate_coders.knowledge.skill_curriculum import (
    STAGES,
    SkillCard,
    SkillCurriculum,
    curriculum_order,
    four_stage_validate,
    skill_curriculum_enabled,
)


def _card(skill_id: str = "skill-1", **overrides) -> SkillCard:
    values = {
        "skill_id": skill_id,
        "trigger": "when a test fails with an import error",
        "procedure": "inspect sys.path and reinstall the package",
        "verification": "run the failing test and the package smoke test",
    }
    values.update(overrides)
    return SkillCard(**values)


def test_card_has_exactly_four_fields() -> None:
    card = _card()
    assert set(card.as_dict()) == {"skill_id", "trigger", "procedure", "verification"}
    assert STAGES == ("schema", "replay", "transfer", "safety")


def test_schema_stage_fails_when_card_incomplete() -> None:
    report = four_stage_validate(
        _card(trigger=""), {"replay": True, "transfer": True, "safety": True}
    )
    assert report.failed_stage == "schema"
    assert report.stages[0].passed is False
    assert "trigger" in report.stages[0].detail


def test_replay_stage_failure() -> None:
    report = four_stage_validate(
        _card(),
        {"schema": True, "replay": "failed", "transfer": True, "safety": True},
    )
    assert report.failed_stage == "replay"
    assert report.stage == "schema"
    assert report.stages[2].detail == "not evaluated"


def test_transfer_and_safety_stage_failures() -> None:
    transfer = four_stage_validate(
        _card(),
        {"schema": True, "replay": True, "safety": True},
    )
    safety = four_stage_validate(
        _card(),
        {"schema": True, "replay": True, "transfer": True},
    )
    assert transfer.failed_stage == "transfer"
    assert safety.failed_stage == "safety"
    assert transfer.passed is False
    assert safety.passed is False


def test_full_validation_passes() -> None:
    report = four_stage_validate(
        _card(),
        {
            "schema": True,
            "replay": ["trace-1"],
            "transfer": {"passed": True},
            "safety": "review-42",
        },
    )
    assert report.passed is True
    assert report.failed_stage is None
    assert report.stage_index == 4


def test_curriculum_order_by_stage_then_priority_then_id() -> None:
    curriculum = SkillCurriculum()
    full = curriculum.add(_card("b-full"), priority=0)
    schema_only = curriculum.add(_card("a-schema"), priority=0)
    curriculum.add(_card("c-high"), priority=5)
    curriculum.add(_card("d-high"), priority=5)

    full_report = curriculum.validate(full, {"replay": True, "transfer": True, "safety": True})
    assert full_report.passed is True
    curriculum.validate(schema_only, {})
    curriculum.validate("c-high", {})
    curriculum.validate("d-high", {})

    ordered = curriculum.curriculum_order()
    assert [card.skill_id for card in ordered] == ["b-full", "c-high", "d-high", "a-schema"]
    assert ordered[0] is full


def test_module_level_order_without_reports_is_idempotent() -> None:
    cards = [_card("z"), _card("a"), _card("m")]
    first = curriculum_order(reversed(cards))
    second = curriculum_order(reversed(cards))
    assert [card.skill_id for card in first] == ["a", "m", "z"]
    assert [card.skill_id for card in first] == [card.skill_id for card in second]


def test_duplicate_id_rejected() -> None:
    curriculum = SkillCurriculum([_card()])
    try:
        curriculum.add(_card())
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("duplicate skill id was accepted")


def test_flag(monkeypatch) -> None:
    monkeypatch.delenv("UC_SKILL_CURRICULUM", raising=False)
    assert skill_curriculum_enabled() is False
    monkeypatch.setenv("UC_SKILL_CURRICULUM", "true")
    assert skill_curriculum_enabled() is True
    monkeypatch.setenv("UC_SKILL_CURRICULUM", "0")
    assert skill_curriculum_enabled() is False
