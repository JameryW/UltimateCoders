"""Tests for STAIR plan reuse on retries (v19 #74 wiring).

The hierarchy itself is covered by ``test_v19_stair_tree.py``.  These tests
cover the projection from real attempt rounds onto the three STAIR levels and
the additive context item the worker builds on a retry.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import ultimate_coders.agent.worker as worker_mod
from ultimate_coders.agent.context_compiler import ContextItem, Tier
from ultimate_coders.agent.plan_reuse import (
    MAX_PLAN_NODES,
    PLAN_REUSE_ENV,
    AttemptSketch,
    node_levels,
    plan_block,
    plan_reuse_enabled,
    plan_tags,
    sketch_from_round,
)
from ultimate_coders.agent.plan_reuse import plan_block as _plan_block
from ultimate_coders.agent.worker import Worker
from ultimate_coders.search.stair_tree import STAIR_TREE_ENV


def _round(
    *,
    attempt: int = 1,
    outcome: str = "failure",
    summary: str = "",
    failure: str = "",
    patches: tuple[tuple[str, str], ...] = (),
    verdict: str = "",
) -> SimpleNamespace:
    files = [
        SimpleNamespace(path=path, change_type=kind, diff="") for path, kind in patches
    ]
    return SimpleNamespace(
        attempt=SimpleNamespace(attempt=attempt, dispatch_attempt=0),
        outcome=outcome,
        summary=summary,
        failure=failure,
        test_verdict=verdict,
        patches=tuple(files),
    )


class TestFlag:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_both_flags_enable_plan_reuse(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PLAN_REUSE_ENV, value)
        monkeypatch.setenv(STAIR_TREE_ENV, "1")
        assert plan_reuse_enabled() is True

    def test_requires_the_stair_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PLAN_REUSE_ENV, "1")
        monkeypatch.delenv(STAIR_TREE_ENV, raising=False)
        assert plan_reuse_enabled() is False

    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(PLAN_REUSE_ENV, raising=False)
        monkeypatch.setenv(STAIR_TREE_ENV, "1")
        assert plan_reuse_enabled() is False


class TestSketch:
    def test_summary_becomes_the_strategy(self) -> None:
        sketch = sketch_from_round(_round(summary="retry the parser path"))
        assert isinstance(sketch, AttemptSketch)
        assert sketch.strategy == "attempt 1 failure: retry the parser path"
        assert sketch.success is False

    def test_first_failure_line_is_the_strategy_without_a_summary(self) -> None:
        sketch = sketch_from_round(_round(failure="boom at line 3\ntrace tail"))
        assert sketch is not None
        assert sketch.strategy == "attempt 1 failure: boom at line 3"
        assert sketch.diagnostics == ("trace tail",)

    def test_patched_paths_become_subgoals(self) -> None:
        sketch = sketch_from_round(
            _round(
                summary="s",
                patches=(("src/cache.py", "modified"), ("src/api.py", "added")),
            )
        )
        assert sketch is not None
        assert sketch.files == ("src/cache.py", "src/api.py")

    def test_test_verdict_becomes_a_diagnostic(self) -> None:
        sketch = sketch_from_round(_round(summary="s", verdict="1 failed, 2 passed"))
        assert sketch is not None
        assert sketch.diagnostics == ("tests: 1 failed, 2 passed",)

    def test_success_outcomes_are_recognised(self) -> None:
        sketch = sketch_from_round(_round(outcome="resolved", summary="fixed"))
        assert sketch is not None and sketch.success is True

    def test_empty_round_is_skipped(self) -> None:
        assert sketch_from_round(_round(summary="", failure="")) is None

    @pytest.mark.parametrize(
        "failure",
        ["x" * 400, "a\nb\nc\nd\ne"],
    )
    def test_text_is_clipped(self, failure: str) -> None:
        sketch = sketch_from_round(_round(failure=failure))
        assert sketch is not None
        assert len(sketch.strategy) <= 200
        assert len(sketch.diagnostics) <= 2


class TestPlanBlock:
    def test_empty_rounds_render_nothing(self) -> None:
        assert plan_block("fix cache", []) is None

    def test_unusable_rounds_render_nothing(self) -> None:
        assert plan_block("fix cache", [_round(summary="", failure="")]) is None

    def test_plan_has_all_three_levels(self) -> None:
        rounds = [
            _round(
                summary="invalidate cache",
                failure="AssertionError: stale read",
                patches=(("src/cache.py", "modified"),),
                verdict="1 failed",
            )
        ]
        result = plan_block("invalidate cache on write", rounds)
        assert result is not None
        text, meta = result
        assert "What was tried: attempt 1 failure: invalidate cache" in text
        assert "Next: edit src/cache.py" in text
        assert "Known failure: AssertionError: stale read" in text
        assert meta["attempts"] == 1 and meta["failed_attempts"] == 1
        assert 0 < meta["nodes"] <= MAX_PLAN_NODES

    def test_node_budget_is_respected(self) -> None:
        rounds = [
            _round(
                summary=f"attempt {index}",
                failure="boom",
                patches=tuple((f"src/m{index}.py", "modified") for index in range(3)),
                verdict="failed",
            )
            for index in range(1, 4)
        ]
        result = plan_block("boom", rounds, max_nodes=2)
        assert result is not None
        assert result[1]["nodes"] <= 2

    def test_only_the_last_attempts_are_used(self) -> None:
        rounds = [
            _round(attempt=index, summary=f"attempt {index}") for index in range(1, 10)
        ]
        result = plan_block("attempt", rounds, max_attempts=3)
        assert result is not None
        assert result[1]["attempts"] == 3

    def test_meta_is_json_ready(self) -> None:
        result = plan_block("x", [_round(summary="s")])
        assert result is not None
        assert json.loads(json.dumps(result[1]))["strategy"].startswith("attempt 1")


class TestResources:
    def test_tag_helpers(self) -> None:
        rounds = [
            _round(summary="did it", outcome="success"),
            _round(attempt=2, summary="broke", outcome="failure"),
        ]
        result = _plan_block("did it broke", rounds)
        assert result is not None
        tree = None  # helpers below only need nodes from a fresh search
        from ultimate_coders.agent.plan_reuse import _build_tree  # noqa: PLC0415

        sketches = [sketch_from_round(item) for item in rounds]
        tree = _build_tree(tuple(sketch for sketch in sketches if sketch))
        plan = tree.search("did it broke", include_failed=True)
        assert "success" in plan_tags(plan) or "failure" in plan_tags(plan)
        assert node_levels(plan.nodes)["strategy"] == 1
        assert tree is not None


class TestRecoveryWiring:
    def _view(self, rounds: list[object]) -> SimpleNamespace:
        return SimpleNamespace(rounds=tuple(rounds), summary={"attempts": 1})

    async def test_plan_item_is_appended_on_a_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UC_PLAN_REUSE", "1")
        monkeypatch.setenv(STAIR_TREE_ENV, "1")
        monkeypatch.delenv("UC_AST_EVOLUTION", raising=False)
        rounds = [
            _round(
                summary="invalidate cache",
                failure="AssertionError: stale read",
                patches=(("src/cache.py", "modified"),),
            )
        ]
        worker = Worker.__new__(Worker)
        worker._load_recovery_view = AsyncMock(return_value=self._view(rounds))
        worker._last_route = SimpleNamespace(risk="medium")
        worker._last_context_manifest = None
        worker._assembled_context_block = lambda manifest: None
        subtask = SimpleNamespace(
            id="s1",
            parent_id="t1",
            description="invalidate the cache on write",
            dispatch_retry_count=1,
        )
        base_item = ContextItem(item_id="base", tier=Tier.EVIDENCE, content="base")
        with patch.object(
            worker_mod, "recovery_context_items", return_value=[base_item]
        ):
            block, summary = await worker._recovery_context_block(
                subtask,
                1,
                [],
                "BASE",
                legacy_style="worker",
                gateway_truncated=False,
            )
        assert summary["stair_plan"]["attempts"] == 1
        assert "Reusable repair plan" in block
        assert "Next: edit src/cache.py" in block
        assert "base" in block

    async def test_disabled_flag_keeps_the_block_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("UC_PLAN_REUSE", raising=False)
        monkeypatch.setenv(STAIR_TREE_ENV, "1")
        worker = Worker.__new__(Worker)
        worker._load_recovery_view = AsyncMock(
            return_value=self._view([_round(summary="s")])
        )
        worker._last_route = SimpleNamespace(risk="medium")
        worker._last_context_manifest = None
        worker._assembled_context_block = lambda manifest: None
        subtask = SimpleNamespace(
            id="s1",
            parent_id="t1",
            description="invalidate the cache",
            dispatch_retry_count=1,
        )
        base_item = ContextItem(item_id="base", tier=Tier.EVIDENCE, content="base")
        with patch.object(
            worker_mod, "recovery_context_items", return_value=[base_item]
        ):
            block, summary = await worker._recovery_context_block(
                subtask,
                1,
                [],
                "BASE",
                legacy_style="worker",
                gateway_truncated=False,
            )
        assert "stair_plan" not in summary
        assert "Reusable repair plan" not in block
