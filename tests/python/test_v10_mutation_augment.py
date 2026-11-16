"""Tests for deterministic v10 mutation augmentation."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.mutation_augment import (
    Mutation,
    MutationBatch,
    MutationKind,
    generate_mutations,
    mutant_kill_rate,
    mutation_augment_enabled,
)


def _patch() -> tuple[dict[str, str], dict[str, str]]:
    before = {"app.py": "def total(items):\n    return sum(items)\n"}
    after = {
        "app.py": (
            "def total(items):\n"
            "    # must return the total\n"
            "    return sum(items) + 1\n"
        )
    }
    return before, after


def test_all_three_mutation_kinds_can_be_generated() -> None:
    before, after = _patch()

    batch = generate_mutations(before, after)

    assert isinstance(batch, MutationBatch)
    assert batch.kinds == {
        MutationKind.SECTION_REVERT,
        MutationKind.REQUIREMENT_MISMATCH,
        MutationKind.SEMANTIC_MUTATION,
    }
    assert all(isinstance(item, Mutation) for item in batch.mutations)


def test_mutation_output_is_deterministic() -> None:
    before, after = _patch()

    first = generate_mutations(before, after)
    second = generate_mutations(before, after)

    assert first.to_dict() == second.to_dict()
    assert [item.after for item in first.mutations] == [item.after for item in second.mutations]


def test_section_revert_restores_pre_patch_text() -> None:
    before, after = _patch()

    batch = generate_mutations(before, after)
    reverts = batch.by_kind(MutationKind.SECTION_REVERT)

    assert reverts
    assert reverts[0].before == after["app.py"]
    assert reverts[0].after != reverts[0].before


def test_mutant_kill_rate_math() -> None:
    assert mutant_kill_rate(0, 0) == 0.0
    assert mutant_kill_rate(1, 4) == 25.0
    assert mutant_kill_rate(3, 4) == 75.0
    assert mutant_kill_rate(4, 4) == 100.0
    assert mutant_kill_rate(5, 4) == 100.0


def test_batch_kill_rate_uses_batch_size() -> None:
    before, after = _patch()
    batch = generate_mutations(before, after)

    assert batch.kill_rate(len(batch)) == 100.0
    assert batch.kill_rate(0) == 0.0


def test_empty_patch_yields_empty_batch() -> None:
    batch = generate_mutations({"app.py": "value = 1\n"}, {"app.py": "value = 1\n"})

    assert len(batch) == 0
    assert batch.kinds == set()


def test_mutation_augment_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_MUTATION_AUGMENT", raising=False)
    assert mutation_augment_enabled() is False
    monkeypatch.setenv("UC_MUTATION_AUGMENT", "1")
    assert mutation_augment_enabled() is True
    monkeypatch.setenv("UC_MUTATION_AUGMENT", "no")
    assert mutation_augment_enabled() is False
