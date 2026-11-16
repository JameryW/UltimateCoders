"""Tests for fixed-prefix v10 context assembly."""

from __future__ import annotations

import hashlib

import pytest
from ultimate_coders.search.context_assembler import (
    CHECKPOINT,
    DECLARATIVE,
    OBSERVATION,
    PROCEDURAL,
    SLOT_ORDER,
    SYSTEM_SCHEMA,
    AssembledContext,
    ContextAssembler,
    ContextSlot,
    assemble_context,
    context_assembler_enabled,
    estimate_tokens,
)


def _five_slots(*, observation: str = "tool output") -> list[ContextSlot]:
    return [
        ContextSlot(SYSTEM_SCHEMA, "system schema"),
        ContextSlot(DECLARATIVE, "repository facts"),
        ContextSlot(PROCEDURAL, "task procedure"),
        ContextSlot(CHECKPOINT, "checkpoint summary"),
        ContextSlot(OBSERVATION, observation, stable=False),
    ]


def test_assemble_fixed_slot_order() -> None:
    shuffled = list(reversed(_five_slots()))

    assembled = ContextAssembler().assemble(shuffled)

    assert [block.slot for block in assembled.blocks] == list(SLOT_ORDER)
    assert [block.stable for block in assembled.blocks] == [True, True, True, True, False]


def test_hashes_are_deterministic() -> None:
    first = assemble_context(_five_slots())
    second = assemble_context(_five_slots())

    assert first.block_sha256 == second.block_sha256
    assert first.full_sha256 == second.full_sha256
    assert first.prefix_cache_hint == second.prefix_cache_hint
    assert first.block_sha256[0] == first.blocks[0].sha256


def test_unchanged_prefix_only_observation_changes() -> None:
    previous = assemble_context(_five_slots(observation="old observation"))
    current = assemble_context(_five_slots(observation="new observation"))

    changed = current.changed_suffix(previous)
    assert [block.slot for block in changed] == [OBSERVATION]
    assert current.prefix_cache_hint == previous.prefix_cache_hint
    assert current.full_sha256 != previous.full_sha256


def test_suffix_invalidation_when_stable_slot_changes() -> None:
    previous = assemble_context(_five_slots())
    slots = _five_slots()
    slots[1] = ContextSlot(DECLARATIVE, "different repository facts")
    current = assemble_context(slots)

    changed = current.changed_suffix(previous)
    assert [block.slot for block in changed] == [DECLARATIVE, PROCEDURAL, CHECKPOINT, OBSERVATION]
    assert current.prefix_cache_hint != previous.prefix_cache_hint


def test_budget_truncates_observation_tail_only() -> None:
    slots = [
        ContextSlot(SYSTEM_SCHEMA, "s" * 40),
        ContextSlot(DECLARATIVE, "d" * 40),
        ContextSlot(OBSERVATION, "x" * 1000, stable=False),
    ]

    assembled = ContextAssembler().assemble(slots, budget_tokens=30)

    assert assembled.total_tokens == 30
    assert assembled.over_budget is False
    assert assembled.truncated is True
    observation = assembled.blocks[-1]
    assert observation.slot == OBSERVATION
    assert observation.truncated is True
    assert len(observation.content) == 40
    assert assembled.blocks[0].content == "s" * 40
    assert assembled.blocks[1].content == "d" * 40
    expected = hashlib.sha256(observation.content.encode()).hexdigest()
    assert observation.sha256 == expected


def test_stable_blocks_are_never_truncated() -> None:
    slots = [
        ContextSlot(SYSTEM_SCHEMA, "s" * 80),
        ContextSlot(OBSERVATION, "x" * 80, stable=False),
    ]

    assembled = ContextAssembler().assemble(slots, budget_tokens=10)

    assert assembled.blocks[0].content == "s" * 80
    assert assembled.total_tokens > assembled.budget_tokens
    assert assembled.over_budget is True


def test_empty_previous_reports_all_blocks_changed() -> None:
    assembled = assemble_context(_five_slots())

    assert assembled.changed_suffix(None) == assembled.blocks
    assert isinstance(assembled, AssembledContext)


def test_estimate_tokens_uses_four_chars_per_token() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_duplicate_and_unknown_slots_are_rejected() -> None:
    with pytest.raises(ValueError):
        assemble_context(
            [ContextSlot(SYSTEM_SCHEMA, "one"), ContextSlot(SYSTEM_SCHEMA, "two")]
        )
    with pytest.raises(ValueError):
        ContextSlot("not_a_slot", "content")


def test_context_assembler_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_PREFIX_CONTEXT_ASSEMBLY", raising=False)
    assert context_assembler_enabled() is False
    monkeypatch.setenv("UC_PREFIX_CONTEXT_ASSEMBLY", "true")
    assert context_assembler_enabled() is True
    monkeypatch.setenv("UC_PREFIX_CONTEXT_ASSEMBLY", "off")
    assert context_assembler_enabled() is False
