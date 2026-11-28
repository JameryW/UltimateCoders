"""Tests for ledger #109 AgentPRO compiler."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.agentpro_compiler import (
    AGENTPRO_COMPILER_ENV,
    AgentProCompiler,
    AgentProError,
    WorkflowOp,
    agentpro_compiler_enabled,
)


def op(op_id: str, kind: str = "llm", **kwargs: object) -> WorkflowOp:
    return WorkflowOp(id=op_id, kind=kind, **kwargs)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENTPRO_COMPILER_ENV, raising=False)
    assert agentpro_compiler_enabled() is False
    monkeypatch.setenv(AGENTPRO_COMPILER_ENV, "yes")
    assert agentpro_compiler_enabled() is True


def test_empty_workflow_is_rejected() -> None:
    with pytest.raises(AgentProError):
        AgentProCompiler().compile([])


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(AgentProError):
        AgentProCompiler().compile([op("x"), op("x")])


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(AgentProError):
        WorkflowOp(id="x", kind="teleport")


def test_single_llm_region_gets_guard() -> None:
    result = AgentProCompiler().compile([op("x", token_cost=100)])
    assert len(result.regions) == 1
    assert result.regions[0].fused is False
    assert result.regions[0].guard == "guard:x"
    assert result.fallback_path == ("guard:x",)


def test_redundant_read_is_removed() -> None:
    ops = [
        op("load", "tool", writes=("cache",)),
        op("use", "tool", reads=("cache",)),
    ]
    result = AgentProCompiler().compile(ops)
    assert result.removed_reads == ("cache",)
    assert result.compiled_tokens < result.original_tokens


def test_consecutive_llm_calls_are_fused() -> None:
    result = AgentProCompiler().compile([op("a"), op("b"), op("c")])
    assert len(result.regions) == 1
    assert result.regions[0].fused is True
    assert result.regions[0].op_ids == ("a", "b", "c")


def test_observation_prevents_fusion() -> None:
    result = AgentProCompiler().compile(
        [op("a"), WorkflowOp(id="b", kind="llm", observes_previous=True)]
    )
    assert [region.fused for region in result.regions] == [False, False]


def test_guard_path_has_one_entry_per_region() -> None:
    result = AgentProCompiler().compile([op("a"), op("inspect", "read")])
    assert len(result.fallback_path) == len(result.regions)


def test_token_saving_is_reported() -> None:
    result = AgentProCompiler().compile([op("a", token_cost=100), op("b", token_cost=100)])
    assert result.original_tokens == 200
    assert 0 < result.token_saving_ratio < 1
    assert result.to_dict()["removed_reads"] == []
