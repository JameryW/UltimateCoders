from __future__ import annotations

from ultimate_coders.knowledge.skill_plumber import (
    CoverageMatrix,
    Instruction,
    InstructionStatus,
    Observation,
    PolicyBank,
    QuestionKind,
    SkillPlumber,
    skill_plumber_enabled,
)


def test_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("UC_SKILL_PLUMBER", raising=False)
    assert skill_plumber_enabled() is False
    monkeypatch.setenv("UC_SKILL_PLUMBER", "yes")
    assert skill_plumber_enabled() is True


def test_analyze_classifies_each_instruction():
    plumber = SkillPlumber()
    instructions = (
        Instruction("i1", "read the failing test"),
        Instruction("i2", "run the full suite"),
        Instruction("i3", "never edit tests"),
    )
    observations = (
        Observation("i1", InstructionStatus.FOLLOW),
        Observation("i2", InstructionStatus.NOT_FOLLOW),
        Observation("i3", InstructionStatus.NOT_COVERED),
    )
    matrix = plumber.analyze("skill-a", instructions, observations)
    assert isinstance(matrix, CoverageMatrix)
    assert matrix.by_instruction("i1").status is InstructionStatus.FOLLOW
    assert matrix.by_instruction("i2").status is InstructionStatus.NOT_FOLLOW
    assert matrix.by_instruction("i3").status is InstructionStatus.NOT_COVERED
    assert matrix.coverage == 1.0
    assert matrix.follow_rate == 0.5


def test_partial_coverage_is_reported_as_follow_plus_not_covered():
    plumber = SkillPlumber()
    matrix = plumber.analyze(
        "skill-a",
        (Instruction("i1", "run tests"),),
        (
            Observation("i1", InstructionStatus.FOLLOW),
            Observation("i1", InstructionStatus.NOT_COVERED),
        ),
    )
    report = matrix.by_instruction("i1")
    assert report.status is InstructionStatus.FOLLOW
    assert report.not_covered == 1


def test_unknown_instruction_is_rejected():
    plumber = SkillPlumber()
    try:
        plumber.analyze(
            "skill-a",
            (Instruction("i1", "run tests"),),
            (Observation("missing", InstructionStatus.FOLLOW),),
        )
    except ValueError as error:
        assert "unknown instruction" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_duplicate_and_empty_instructions_are_rejected():
    plumber = SkillPlumber()
    for instructions in (
        (),
        (Instruction("i1", "a"), Instruction("i1", "b")),
    ):
        try:
            plumber.analyze("skill-a", instructions, ())
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_questions_distinguish_execution_and_coverage_gaps():
    plumber = SkillPlumber()
    instructions = (
        Instruction("i1", "read the test"),
        Instruction("i2", "run the suite", critical=True),
        Instruction("i3", "never edit tests"),
    )
    matrix = plumber.analyze(
        "skill-a",
        instructions,
        (
            Observation("i1", InstructionStatus.FOLLOW),
            Observation("i2", InstructionStatus.NOT_FOLLOW),
            Observation("i3", InstructionStatus.NOT_COVERED),
        ),
    )
    questions = plumber.questions(matrix, instructions)
    by_id = {q.instruction_id: q for q in questions}
    assert by_id["i2"].kind is QuestionKind.REINFORCE_EXECUTION
    assert by_id["i2"].critical is True
    assert by_id["i3"].kind is QuestionKind.ADD_COVERAGE
    assert "i1" not in by_id


def test_partial_coverage_asks_for_more_fixtures():
    plumber = SkillPlumber()
    matrix = plumber.analyze(
        "skill-a",
        (Instruction("i1", "run tests"),),
        (
            Observation("i1", InstructionStatus.FOLLOW),
            Observation("i1", InstructionStatus.NOT_COVERED),
        ),
    )
    question = plumber.questions(matrix)[0]
    assert question.kind is QuestionKind.ADD_COVERAGE


def test_validate_marks_only_a_followed_round_valid():
    plumber = SkillPlumber()
    question = plumber.questions(
        plumber.analyze(
            "skill-a",
            (Instruction("i1", "run tests"),),
            (Observation("i1", InstructionStatus.NOT_FOLLOW),),
        ),
        (Instruction("i1", "run tests"),),
    )[0]
    assert plumber.validate(question, InstructionStatus.FOLLOW).validated is True
    assert plumber.validate(question, InstructionStatus.NOT_FOLLOW).validated is False


def test_distill_only_keeps_validated_questions_in_bank():
    plumber = SkillPlumber()
    question = plumber.questions(
        plumber.analyze(
            "skill-a",
            (Instruction("i1", "run tests"),),
            (Observation("i1", InstructionStatus.NOT_FOLLOW),),
        ),
        (Instruction("i1", "run tests"),),
    )[0]
    entries = plumber.distill((question,), {question.id: True})
    assert len(entries) == 1
    assert len(plumber.bank) == 1
    assert "validated by a later round" in entries[0].text
    assert plumber.distill((question,), {question.id: False}) == ()


def test_policy_bank_is_bounded_and_rejects_duplicates():
    plumber = SkillPlumber()
    question = plumber.questions(
        plumber.analyze(
            "skill-a",
            (Instruction("i1", "run tests"),),
            (Observation("i1", InstructionStatus.NOT_FOLLOW),),
        ),
        (Instruction("i1", "run tests"),),
    )[0]
    assert plumber.distill((question,), {question.id: True})
    assert plumber.distill((question,), {question.id: True})
    assert len(plumber.bank) == 1

    bank = PolicyBank(max_entries=1)
    entries = plumber.distill((question,), {question.id: True})
    bank.add(entries[0])
    assert bank.add(entries[0]) is False
