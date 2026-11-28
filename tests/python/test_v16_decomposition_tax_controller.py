"""Tests for the decomposition-tax controller (ledger #56)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.decomposition_tax_controller import (
    DECOMPOSITION_TAX_CONTROLLER_ENV,
    DecompositionError,
    DecompositionTaxController,
    PipelinePlan,
    Regrounding,
    StageSpec,
    decomposition_tax_enabled,
    reground_after_lossy_stage,
)


def plan(*sizes: int, condensing: tuple[int, ...] = ()) -> PipelinePlan:
    stages = tuple(
        StageSpec(f"stage{index}", size, index in condensing)
        for index, size in enumerate(sizes)
    )
    return PipelinePlan(stages)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DECOMPOSITION_TAX_CONTROLLER_ENV, raising=False)
    assert decomposition_tax_enabled() is False
    monkeypatch.setenv(DECOMPOSITION_TAX_CONTROLLER_ENV, "on")
    assert decomposition_tax_enabled() is True


def test_expansion_factor_uses_first_stage() -> None:
    assert plan(100, 50, 50).expansion_factor == 2.0
    assert plan(100).expansion_factor == 1.0


def test_pipeline_is_chosen_below_break_even() -> None:
    controller = DecompositionTaxController(price_ratio=0.25)
    decision = controller.decide(plan(100, 50))
    assert decision.uses_pipeline is True
    assert decision.route == "pipeline"
    assert decision.break_even_alpha == 4.0
    assert "below" in decision.reason


def test_equality_routes_to_single_call() -> None:
    controller = DecompositionTaxController(price_ratio=0.5)
    decision = controller.decide(plan(100, 100))
    assert decision.alpha == 2.0
    assert decision.break_even_alpha == 2.0
    assert decision.route == "single_call"
    assert "meets or exceeds" in decision.reason


def test_large_expansion_routes_to_single_call() -> None:
    decision = DecompositionTaxController(price_ratio=0.5).decide(plan(100, 300))
    assert decision.route == "single_call"


def test_price_ratio_must_be_positive() -> None:
    with pytest.raises(DecompositionError):
        DecompositionTaxController(price_ratio=0.0)
    with pytest.raises(DecompositionError):
        DecompositionTaxController(price_ratio=-1.0)


def test_stage_validation() -> None:
    with pytest.raises(DecompositionError):
        StageSpec("", 10)
    with pytest.raises(DecompositionError):
        StageSpec("stage", 0)
    with pytest.raises(DecompositionError):
        PipelinePlan(())
    with pytest.raises(DecompositionError):
        PipelinePlan((StageSpec("a", 1), StageSpec("a", 1)))


def test_lossy_indices_skip_the_first_stage() -> None:
    pipeline = PipelinePlan(
        (
            StageSpec("seed", 10, condensing=True),
            StageSpec("summary", 5, condensing=True),
            StageSpec("edit", 5),
            StageSpec("verify", 5, condensing=True),
        )
    )
    assert pipeline.lossy_stage_indices == (1, 3)


def test_regrounding_is_required_only_after_lossy_stage() -> None:
    controller = DecompositionTaxController()
    pipeline = PipelinePlan(
        (
            StageSpec("seed", 10),
            StageSpec("summary", 5, condensing=True),
            StageSpec("edit", 5),
        )
    )
    assert controller.reground(pipeline, 0, "issue", "downstream") is None
    assert controller.reground(pipeline, 2, "issue", "downstream") is None
    block = controller.reground(pipeline, 1, "issue", "downstream")
    assert isinstance(block, Regrounding)
    assert block.lossy_stage == "summary"


def test_regrounding_renders_issue_and_relation_clause() -> None:
    text = reground_after_lossy_stage(
        "Fix the cache invalidation bug.",
        "The summary says edit cache.py.",
        "summarize",
    )
    assert "Fix the cache invalidation bug." in text
    assert "The summary says edit cache.py." in text
    assert "Preserve every entity, number, identifier" in text
    assert "re-anchored after lossy stage: summarize" in text


def test_regrounding_can_drop_relation_clause() -> None:
    block = Regrounding("issue", "downstream", "summary", preserve_relations=False)
    assert "Preserve every entity and identifier." in block.render()


def test_reground_many_matches_lossy_stages() -> None:
    controller = DecompositionTaxController()
    pipeline = PipelinePlan(
        (
            StageSpec("seed", 10),
            StageSpec("summary", 5, condensing=True),
            StageSpec("edit", 5, condensing=True),
        )
    )
    blocks = controller.reground_many(pipeline, "issue", ("first", "second"))
    assert [block.downstream_input for block in blocks] == ["first", "second"]
    with pytest.raises(DecompositionError):
        controller.reground_many(pipeline, "issue", ("only-one",))


def test_out_of_range_stage_is_rejected() -> None:
    controller = DecompositionTaxController()
    pipeline = plan(10)
    with pytest.raises(DecompositionError):
        controller.reground(pipeline, -1, "issue", "downstream")
    with pytest.raises(DecompositionError):
        controller.reground(pipeline, 1, "issue", "downstream")


def test_serialization_is_bounded() -> None:
    pipeline = plan(10, 5, condensing=(1,))
    decision = DecompositionTaxController().decide(pipeline)
    assert pipeline.to_dict()["expansion_factor"] == 1.5
    assert decision.to_dict()["route"] == "pipeline"
    block = Regrounding("issue", "downstream", "summary")
    assert block.to_dict()["lossy_stage"] == "summary"
