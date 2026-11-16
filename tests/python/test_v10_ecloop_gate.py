"""Tests for the v10 ECLoop evidence-conditioned execution gate (#19)."""

from __future__ import annotations

from ultimate_coders.agent.ecloop_gate import (
    DEFAULT_MAX_BLOCKS,
    ECLoopGate,
    EvidenceGap,
    ecloop_gate_enabled,
)


def test_missing_evidence_blocks_edit() -> None:
    gate = ECLoopGate()
    decision = gate.evaluate(
        "pkg/mod.py",
        "Widget.render",
        "edit",
        [],
        callers=("pkg/app.py::main",),
        tests=("tests/test_mod.py::test_render",),
    )
    assert decision.allowed is False
    assert decision.attempts == 1
    assert decision.gap is not None
    assert isinstance(decision.gap, EvidenceGap)
    assert decision.gap.target == "pkg/mod.py::Widget.render"
    assert decision.gap.requested_action == "edit"
    assert any("caller" in item for item in decision.gap.missing)
    assert "ECLoop evidence gate" in decision.prompt
    assert "blocked" in decision.to_tool_error()


def test_caller_evidence_allows_edit() -> None:
    gate = ECLoopGate()
    decision = gate.evaluate(
        "pkg/mod.py",
        "Widget.render",
        "edit",
        ["pkg/app.py::main"],
        callers=("pkg/app.py::main",),
    )
    assert decision.allowed is True
    assert decision.gap is None
    assert decision.attempts == 0
    assert decision.to_tool_error() == ""


def test_override_or_test_evidence_also_allows() -> None:
    gate = ECLoopGate()
    override = gate.evaluate(
        "pkg/mod.py",
        "Base.run",
        "submit",
        ["pkg/child.py::Child.run"],
        overrides=("pkg/child.py::Child.run",),
    )
    test_only = gate.evaluate(
        "pkg/mod.py",
        "Base.run",
        "submit",
        ["tests/test_mod.py::test_run"],
        tests=("tests/test_mod.py::test_run",),
    )
    assert override.allowed is True
    assert test_only.allowed is True


def test_max_three_blocks_then_lenient_allows_with_warning() -> None:
    gate = ECLoopGate(strict=False)
    for expected in (1, 2, 3):
        decision = gate.evaluate("pkg/mod.py", "f", "edit", [], callers=("a.py::g",))
        assert decision.allowed is False
        assert decision.attempts == expected
    assert DEFAULT_MAX_BLOCKS == 3
    allowed = gate.evaluate("pkg/mod.py", "f", "edit", [], callers=("a.py::g",))
    assert allowed.allowed is True
    assert allowed.attempts == 3
    assert "lenient" in allowed.warning
    assert allowed.gap is not None


def test_strict_stays_blocked_after_budget() -> None:
    gate = ECLoopGate(strict=True)
    for _ in range(DEFAULT_MAX_BLOCKS):
        assert gate.evaluate("pkg/mod.py", "f", "edit", []).allowed is False
    denied = gate.evaluate("pkg/mod.py", "f", "edit", [])
    assert denied.allowed is False
    assert denied.attempts == DEFAULT_MAX_BLOCKS
    assert "strict" in denied.warning
    assert "budget exhausted" in denied.prompt.lower()


def test_prompt_contains_target_and_requested_action() -> None:
    gate = ECLoopGate()
    decision = gate.evaluate("pkg/mod.py", "Widget.render", "submit", [])
    assert "pkg/mod.py::Widget.render" in decision.prompt
    assert "'submit'" in decision.prompt
    assert "1-hop" in decision.prompt
    assert len(decision.prompt) <= 900


def test_unrelated_evidence_does_not_satisfy_known_candidates() -> None:
    gate = ECLoopGate()
    decision = gate.evaluate(
        "pkg/mod.py",
        "f",
        "edit",
        ["unrelated.py::other"],
        callers=("a.py::g",),
        tests=("tests/test_mod.py::test_f",),
    )
    assert decision.allowed is False
    assert decision.attempts == 1


def test_non_guarded_actions_are_free() -> None:
    gate = ECLoopGate()
    decision = gate.evaluate("pkg/mod.py", "f", "read", [])
    assert decision.allowed is True
    assert decision.attempts == 0
    assert gate.blocks == {}


def test_flag(monkeypatch) -> None:
    monkeypatch.delenv("UC_ECLOOP_GATE", raising=False)
    assert ecloop_gate_enabled() is False
    monkeypatch.setenv("UC_ECLOOP_GATE", "1")
    assert ecloop_gate_enabled() is True
    monkeypatch.setenv("UC_ECLOOP_GATE", "off")
    assert ecloop_gate_enabled() is False
