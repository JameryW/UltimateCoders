"""Tests for SkillForge cold-start distillation (ledger #63)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.skillforge_coldstart import (
    SKILLFORGE_COLDSTART_ENV,
    CardScope,
    MutationKind,
    RepairTrace,
    SkillCard,
    SkillForgeColdStart,
    SkillForgeError,
    SyntheticIssue,
    TestedFunction,
    skillforge_coldstart_enabled,
)


def _function(symbol: str, path: str = "src/mod.py", test: str = "test_mod") -> TestedFunction:
    return TestedFunction(symbol=symbol, path=path, tests=(test,))


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SKILLFORGE_COLDSTART_ENV, raising=False)
    assert not skillforge_coldstart_enabled()
    monkeypatch.setenv(SKILLFORGE_COLDSTART_ENV, "yes")
    assert skillforge_coldstart_enabled()


def test_generate_is_deterministic_and_cycles_mutations() -> None:
    forge = SkillForgeColdStart()
    issues = forge.generate((_function("alpha"), _function("beta")))
    assert [issue.issue_id for issue in issues] == ["synthetic-1", "synthetic-2"]
    assert [issue.mutation for issue in issues] == [
        MutationKind.OFF_BY_ONE,
        MutationKind.NONE_RETURN,
    ]
    assert issues[0].source_test == "test_mod"


def test_generate_respects_budget_and_rejects_bad_input() -> None:
    forge = SkillForgeColdStart(max_issues=1)
    assert len(forge.generate((_function("alpha"), _function("beta")))) == 1
    with pytest.raises(SkillForgeError):
        forge.generate(())
    with pytest.raises(SkillForgeError):
        forge.generate((_function("alpha"), _function("alpha")))
    with pytest.raises(SkillForgeError):
        TestedFunction(symbol="alpha", path="src/a.py", tests=())


def test_distill_success_creates_global_and_local_cards() -> None:
    forge = SkillForgeColdStart()
    trace = RepairTrace(
        issue_id="synthetic-1",
        symbol="alpha",
        path="src/mod.py",
        succeeded=True,
        steps=("run test", "read alpha", "edit boundary"),
    )
    cards = forge.distill(trace)
    assert [card.scope for card in cards] == [CardScope.GLOBAL, CardScope.LOCAL]
    assert cards[0].symbol is None
    assert cards[1].symbol == "alpha"
    assert "synthetic-1" in cards[0].evidence
    assert forge.distill(
        RepairTrace(
            issue_id="synthetic-2",
            symbol="beta",
            path="src/mod.py",
            succeeded=False,
            steps=("run test",),
        )
    ) == ()


def test_register_deduplicates_scope_and_name_and_respects_capacity() -> None:
    forge = SkillForgeColdStart(max_cards=2)
    global_card = SkillCard(
        name="reproduce-before-edit",
        scope=CardScope.GLOBAL,
        guidance="reproduce first",
        evidence=("e1",),
    )
    local_a = SkillCard(
        name="avoid-alpha",
        scope=CardScope.LOCAL,
        guidance="preserve alpha",
        evidence=("e2",),
        symbol="alpha",
    )
    local_b = SkillCard(
        name="avoid-beta",
        scope=CardScope.LOCAL,
        guidance="preserve beta",
        evidence=("e3",),
        symbol="beta",
    )
    assert forge.register((global_card, local_a, global_card)) == 2
    assert forge.register((local_b,)) == 2


def test_inject_prefers_local_symbol_then_global() -> None:
    forge = SkillForgeColdStart()
    forge.register(
        (
            SkillCard(
                name="global-rule",
                scope=CardScope.GLOBAL,
                guidance="reproduce first",
                evidence=("e1",),
            ),
            SkillCard(
                name="alpha-rule",
                scope=CardScope.LOCAL,
                guidance="preserve alpha",
                evidence=("e2",),
                symbol="alpha",
            ),
        )
    )
    rendered = forge.inject("alpha")
    assert "## Project skills (SkillForge)" in rendered
    assert rendered.index("alpha-rule") < rendered.index("global-rule")
    assert "beta" not in rendered
    assert forge.inject("alpha", limit=1).count("- [") == 1
    with pytest.raises(SkillForgeError):
        forge.inject("")
    with pytest.raises(SkillForgeError):
        forge.inject("alpha", limit=0)


def test_inject_without_cards_is_empty() -> None:
    assert SkillForgeColdStart().inject("alpha") == ""


def test_synthetic_issue_serialisation() -> None:
    issue = SyntheticIssue(
        issue_id="synthetic-1",
        symbol="alpha",
        path="src/mod.py",
        mutation=MutationKind.WRONG_OPERATOR,
        description="desc",
        source_test="test_mod",
    )
    payload = issue.to_dict()
    assert payload["mutation"] == "wrong_operator"
    assert payload["symbol"] == "alpha"
