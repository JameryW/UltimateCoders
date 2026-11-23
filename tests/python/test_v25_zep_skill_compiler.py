"""Tests for ledger #111 Zep verified-trace skill compiler."""

from __future__ import annotations

import pytest
from ultimate_coders.search.zep_skill_compiler import (
    ZEP_SKILL_COMPILER_ENV,
    CompiledSkill,
    Curator,
    Reflector,
    SkillTrace,
    TraceEvent,
    ZepSkillCompiler,
    ZepSkillError,
    zep_skill_compiler_enabled,
)


def trace(trace_id: str, *, verified: bool = True, success: bool = True) -> SkillTrace:
    return SkillTrace(
        trace_id=trace_id,
        task="cache invalidation",
        model="qwen",
        verified=verified,
        success=success,
        events=(TraceEvent(0, "inspect", "service.py"), TraceEvent(1, "edit", "service.py")),
    )


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ZEP_SKILL_COMPILER_ENV, raising=False)
    assert zep_skill_compiler_enabled() is False
    monkeypatch.setenv(ZEP_SKILL_COMPILER_ENV, "on")
    assert zep_skill_compiler_enabled() is True


def test_trace_requires_events() -> None:
    with pytest.raises(ZepSkillError):
        SkillTrace("t", "task", "m", True, True, ())


def test_reflector_ignores_unverified_and_failed() -> None:
    assert Reflector().compile([trace("a", verified=False), trace("b", success=False)]) == ()


def test_reflector_requires_two_verified_traces() -> None:
    assert Reflector().compile([trace("a")]) == ()


def test_reflector_requires_matching_sequences() -> None:
    other = SkillTrace(
        "b",
        "cache invalidation",
        "qwen",
        True,
        True,
        (TraceEvent(0, "search", "service.py"),),
    )
    assert Reflector().compile([trace("a"), other]) == ()


def test_compiled_skill_has_evidence() -> None:
    skills = Reflector().compile([trace("a"), trace("b")])
    assert len(skills) == 1
    assert skills[0].name == "skill:cache-invalidation"
    assert len(skills[0].evidence) == 4


def test_curator_rejects_unsafe_skill() -> None:
    curator = Curator()
    assert curator.admit(CompiledSkill("bad", "bad", ("rm -rf /",))) is False


def test_curator_records_verified_uses() -> None:
    curator = Curator()
    skill = CompiledSkill("s", "summary", ("inspect", "edit"))
    assert curator.admit(skill)
    curator.record_use("s", True)
    curator.record_use("s", False)
    assert curator.rank()[0].win_rate == 0.5


def test_expose_respects_budget() -> None:
    curator = Curator()
    curator.admit(CompiledSkill("s", "summary", ("inspect", "edit")))
    text = curator.expose(max_chars=120)
    assert text.startswith("## Verified skills")
    assert len(text) <= 120


def test_compiler_report_counts_admitted() -> None:
    report = ZepSkillCompiler().compile([trace("a"), trace("b")])
    assert report.admitted == 1
    assert report.to_dict()["admitted"] == 1
