"""Tests for the OverServe guard (v29 #137 production wiring).

Covers the pure guard rules (poll backoff, completed-state breaker,
counter-evidence rollback, provider crash fuse) and the two wire points in the
local harness loop: a repeated poll is refused without executing the tool, and
a ``done`` claim is rolled back while a command failure is unresolved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent.overserve_guard import (
    DEFAULT_BASE_DELAY_MS,
    OVERSERVE_GUARD_ENV,
    CompletionSource,
    CounterEvidenceGuard,
    OverServeGuard,
    PollThrottle,
    ProviderCrashFuse,
    overserve_guard_enabled,
    state_key,
)


class TestFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(OVERSERVE_GUARD_ENV, raising=False)
        assert overserve_guard_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_arm_the_guard(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(OVERSERVE_GUARD_ENV, value)
        assert overserve_guard_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "false", "maybe"])
    def test_falsey_values_keep_it_off(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(OVERSERVE_GUARD_ENV, value)
        assert overserve_guard_enabled() is False


class TestStateKey:
    def test_same_arguments_share_a_key(self) -> None:
        assert state_key("read_file", {"path": "a.py"}) == state_key(
            "read_file", {"path": "a.py"}
        )

    def test_argument_order_does_not_matter(self) -> None:
        assert state_key("run_command", {"a": 1, "b": 2}) == state_key(
            "run_command", {"b": 2, "a": 1}
        )

    def test_tool_name_and_arguments_both_matter(self) -> None:
        base = state_key("read_file", {"path": "a.py"})
        assert base != state_key("list_dir", {"path": "a.py"})
        assert base != state_key("read_file", {"path": "b.py"})


class TestPollThrottle:
    def test_first_poll_is_free_and_repeats_back_off(self) -> None:
        throttle = PollThrottle()
        first = throttle.observe("k")
        assert first.allow is True
        assert first.delay_ms == 0
        assert first.reason == "first_poll"

        second = throttle.observe("k")
        assert second.allow is False
        assert second.delay_ms == DEFAULT_BASE_DELAY_MS

        third = throttle.observe("k")
        assert third.allow is False
        assert third.delay_ms == DEFAULT_BASE_DELAY_MS * 2

    def test_backoff_is_capped(self) -> None:
        throttle = PollThrottle(base_delay_ms=100, max_delay_ms=250)
        throttle.observe("k")
        delays = [throttle.observe("k").delay_ms for _ in range(4)]
        assert delays == [100, 200, 250, 250]

    def test_distinct_keys_have_independent_budgets(self) -> None:
        throttle = PollThrottle()
        assert throttle.observe("a").allow is True
        assert throttle.observe("b").allow is True
        assert throttle.observe("a").allow is False

    def test_completed_state_opens_the_breaker(self) -> None:
        throttle = PollThrottle()
        verdict = throttle.observe("k", completed=True)
        assert verdict.allow is True
        assert throttle.is_completed("k") is True
        broken = throttle.observe("k")
        assert broken.allow is False
        assert broken.breaker_open is True
        assert broken.reason == "completed_state_breaker"

    def test_reset_clears_counts_and_breakers(self) -> None:
        throttle = PollThrottle()
        throttle.observe("k", completed=True)
        throttle.reset()
        assert throttle.is_completed("k") is False
        assert throttle.observe("k").allow is True

    def test_invalid_configuration_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PollThrottle(base_delay_ms=0)
        with pytest.raises(ValueError):
            PollThrottle(base_delay_ms=500, max_delay_ms=100)
        with pytest.raises(ValueError):
            PollThrottle().observe("")


class TestCounterEvidence:
    def test_clean_claim_passes(self) -> None:
        guard = CounterEvidenceGuard()
        verdict = guard.evaluate("done:1")
        assert verdict.accepted is True
        assert verdict.rollback is False
        assert verdict.reason == "no_counter_evidence"

    def test_contradiction_rolls_back_once_then_acknowledges(self) -> None:
        guard = CounterEvidenceGuard()
        first = guard.evaluate("done:1", contradicting=("pytest failed",))
        assert first.accepted is False
        assert first.rollback is True
        assert first.reason == "counter_evidence_present"
        assert guard.blocks == 1

        second = guard.evaluate("done:2", contradicting=("pytest failed",))
        assert second.accepted is True
        assert second.reason == "contradiction_acknowledged"

    def test_zero_block_budget_allows_immediately(self) -> None:
        guard = CounterEvidenceGuard(max_blocks=0)
        verdict = guard.evaluate("done:1", contradicting=("x",))
        assert verdict.accepted is True
        assert verdict.rollback is False

    def test_empty_claim_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CounterEvidenceGuard().evaluate("")

    def test_negative_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            CounterEvidenceGuard(max_blocks=-1)


class TestProviderCrashFuse:
    def test_model_completion_is_always_allowed(self) -> None:
        fuse = ProviderCrashFuse()
        fuse.record_crash("ollama")
        assert fuse.completion_allowed("ollama", CompletionSource.MODEL) is True

    def test_synthesis_is_denied_after_a_crash(self) -> None:
        fuse = ProviderCrashFuse()
        assert (
            fuse.completion_allowed("ollama", CompletionSource.FRAMEWORK_SYNTHESIS)
            is True
        )
        fuse.record_crash("ollama")
        assert fuse.crashed("ollama") is True
        assert fuse.crashed_count == 1
        assert (
            fuse.completion_allowed("ollama", CompletionSource.FRAMEWORK_SYNTHESIS)
            is False
        )

    def test_empty_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProviderCrashFuse().record_crash("")


class TestGuardFacade:
    def test_unpolled_tools_are_never_gated(self) -> None:
        guard = OverServeGuard()
        assert guard.gate_tool("write_file", {"path": "a.py"}) is None
        assert guard.gate_tool("write_file", {"path": "a.py"}) is None

    def test_repeated_poll_returns_a_tool_error(self) -> None:
        guard = OverServeGuard()
        assert guard.gate_tool("read_file", {"path": "a.py"}) is None
        blocked = guard.gate_tool("read_file", {"path": "a.py"})
        assert blocked is not None
        payload = json.loads(blocked)
        assert "poll throttled" in payload["error"]
        assert payload["breaker_open"] is False
        assert "Wait at least" in payload["hint"]

    def test_completed_state_breaker_is_reported(self) -> None:
        guard = OverServeGuard()
        guard.notice_completed_state("run_command", {"command": "make"})
        blocked = guard.gate_tool("run_command", {"command": "make"})
        assert blocked is not None
        payload = json.loads(blocked)
        assert payload["breaker_open"] is True
        assert "already reported completion" in payload["hint"]

    def test_completion_is_clean_without_failure(self) -> None:
        guard = OverServeGuard()
        verdict = guard.completion("done:1")
        assert verdict.allowed is True
        assert verdict.reason == "clean"

    def test_unresolved_failure_blocks_then_clears(self) -> None:
        guard = OverServeGuard()
        guard.observe_command_step(failed=True)
        blocked = guard.completion("done:1")
        assert blocked.allowed is False
        assert blocked.rollback is True
        assert "contradicted" in blocked.to_tool_error()

        guard.observe_command_step(failed=False)
        assert guard.completion("done:2").allowed is True

    def test_explicit_contradiction_overrides_tracking(self) -> None:
        guard = OverServeGuard()
        blocked = guard.completion("done:1", contradicting=("suite red",))
        assert blocked.allowed is False

    def test_report_shape(self) -> None:
        guard = OverServeGuard()
        guard.observe_command_step(failed=True)
        guard.completion("done:1")
        report = guard.to_dict()
        assert report["completion_blocks"] == 1
        assert report["unresolved_failure"] is True
        assert report["crashed_providers"] == 0


def _resp(tool_calls: list[tuple[str, dict]], text: str = "") -> SimpleNamespace:
    calls = [
        SimpleNamespace(
            id=f"c{i}",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(tool_calls)
    ]
    choice = SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=calls))
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
    return SimpleNamespace(choices=[choice], usage=usage)


class TestHarnessWiring:
    def test_repeated_poll_is_refused_without_running_the_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(OVERSERVE_GUARD_ENV, "1")
        (tmp_path / "a.py").write_text("print('hi')\n", encoding="utf-8")
        responses = iter(
            [
                _resp([("read_file", {"path": "a.py"})]),
                _resp([("read_file", {"path": "a.py"})]),
                _resp([("done", {"summary": "read it"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["success"] is True
        assert final["overserve"]["completed_states"] == 0
        assert final["overserve"]["completion_blocks"] == 0

    def test_done_is_rolled_back_while_a_command_failure_is_unresolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.setenv(OVERSERVE_GUARD_ENV, "1")
        responses = iter(
            [
                _resp([("run_command", {"command": "pytest -q"})]),
                _resp([("done", {"summary": "all good"})]),
                _resp([("done", {"summary": "all good"})]),
            ]
        )
        failed = json.dumps({"exit_code": 3, "output": "1 failed, 2 passed"})
        with (
            patch.object(hll, "_completion", side_effect=lambda m: next(responses)),
            patch.object(hll, "_tool_run_command", AsyncMock(return_value=failed)),
        ):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        blocks = [ln for ln in lines if ln.get("event") == "overserve_block"]
        assert [row["tool"] for row in blocks] == ["done"]
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert final["overserve"]["completion_blocks"] == 1

    def test_disabled_guard_keeps_the_legacy_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv(OVERSERVE_GUARD_ENV, raising=False)
        (tmp_path / "a.py").write_text("print('hi')\n", encoding="utf-8")
        responses = iter(
            [
                _resp([("read_file", {"path": "a.py"})]),
                _resp([("read_file", {"path": "a.py"})]),
                _resp([("done", {"summary": "read it"})]),
            ]
        )
        with patch.object(hll, "_completion", side_effect=lambda m: next(responses)):
            code = asyncio.run(hll.run_loop("task", str(tmp_path), 10))
        assert code == 0

        lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
        assert [ln for ln in lines if ln.get("event") == "overserve_block"] == []
        final = [ln for ln in lines if ln.get("event") == "final"][-1]
        assert "overserve" not in final
