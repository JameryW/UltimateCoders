"""Tests for ledger item #87 (enterprise DSL compile-edit and skill wording)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.dsl_skill_compiler import (
    CompileResult,
    DslSkillCompiler,
    DslSkillError,
    DslSkillPack,
    DslTask,
    SkillFormat,
    SkillVariant,
    dsl_skill_compiler_enabled,
)


def task() -> DslTask:
    return DslTask("task-1", "create an incident workflow", ("trigger", "close"))


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_DSL_SKILL_COMPILER", raising=False)
    assert not dsl_skill_compiler_enabled()
    monkeypatch.setenv("UC_DSL_SKILL_COMPILER", "on")
    assert dsl_skill_compiler_enabled()


def test_compile_passes_on_the_first_round() -> None:
    compiler = DslSkillCompiler()
    report = compiler.compile_edit(task(), "workflow {}", lambda source: CompileResult(True, 1.0))
    assert report.success
    assert report.rounds == 1
    assert len(report.attempts) == 1


def test_compile_edit_loop_uses_editor() -> None:
    compiler = DslSkillCompiler(max_rounds=3)
    outcomes = iter([CompileResult(False, 0.3, ("missing close",)), CompileResult(True, 1.0)])

    def compile_one(source: str) -> CompileResult:
        return next(outcomes)

    def edit(source: str, result: CompileResult) -> str:
        return source + "\n// " + result.errors[0]

    report = compiler.compile_edit(task(), "start", compile_one, edit)
    assert report.success
    assert report.rounds == 2
    assert "missing close" in report.attempts[1].source


def test_failed_compile_without_editor_stops_immediately() -> None:
    compiler = DslSkillCompiler()
    report = compiler.compile_edit(
        task(), "start", lambda source: CompileResult(False, 0.4, ("bad",))
    )
    assert not report.success
    assert report.rounds == 1
    assert report.best_score == 0.4


def test_best_partial_is_retained() -> None:
    compiler = DslSkillCompiler(max_rounds=2)
    outcomes = iter([CompileResult(False, 0.2, ("bad",)), CompileResult(False, 0.6, ("worse",))])
    report = compiler.compile_edit(
        task(),
        "start",
        lambda source: next(outcomes),
        lambda source, result: source + "\n// revised",
    )
    assert not report.success
    assert report.best_score == 0.6


def test_max_rounds_must_be_positive() -> None:
    with pytest.raises(DslSkillError, match="max_rounds"):
        DslSkillCompiler(max_rounds=0)


def test_distill_skill_chooses_highest_score() -> None:
    compiler = DslSkillCompiler()
    result = compiler.distill_skill(
        (
            SkillVariant("plugin", "plugin text", 0.14),
            SkillVariant("doc", "doc text", 0.28),
            SkillVariant("rewritten", "rewritten text", 0.37),
        )
    )
    assert result.winner.name == "rewritten"
    assert abs(result.delta - 0.23) < 1e-9


def test_distill_skill_accepts_explicit_baseline() -> None:
    compiler = DslSkillCompiler()
    result = compiler.distill_skill(
        (SkillVariant("plugin", "plugin", 0.14), SkillVariant("doc", "doc", 0.28)),
        baseline_name="plugin",
    )
    assert abs(result.delta - 0.14) < 1e-9


def test_reformat_changes_format() -> None:
    compiler = DslSkillCompiler()
    pack = DslSkillPack("skill", SkillFormat.PLUGIN, "instructions")
    converted = compiler.reformat(pack, SkillFormat.DOC)
    assert converted.format == SkillFormat.DOC
    assert converted.instructions == "instructions"


def test_duplicate_variants_are_rejected() -> None:
    compiler = DslSkillCompiler()
    with pytest.raises(DslSkillError, match="unique"):
        compiler.distill_skill(
            (SkillVariant("same", "a", 0.2), SkillVariant("same", "b", 0.3))
        )
