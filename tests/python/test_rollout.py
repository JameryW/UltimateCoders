"""Tests for the knowledge-layer rollout gates (knowledge/rollout.py).

Every flag here is an environment variable that defaults off and fails closed.
The tests pin that behaviour because a typo must never silently enable
knowledge injection into worker prompts.
"""

from __future__ import annotations

from ultimate_coders.knowledge.rollout import (
    ATTEMPT_CHAIN_ROUNDS_ENV,
    DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS,
    DEFAULT_MAX_PATCH_DIFF_CHARS,
    DEFAULT_MAX_PATCH_FILES,
    DEFAULT_RECOVERY_BUDGET_TOKENS,
    MAX_PATCH_DIFF_CHARS_ENV,
    MAX_PATCH_FILES_ENV,
    RECOVERY_BUDGET_ENV,
    STAGE_ENV,
    KnowledgeStage,
    canonical_enabled,
    capture_enabled,
    chain_max_rounds,
    describe_stage,
    graph_sharing_enabled,
    max_patch_diff_chars,
    max_patch_files,
    recovery_budget_tokens,
    recovery_enabled,
    resolve_stage,
    stage_from_env,
)


def test_resolve_stage_understands_named_and_numeric_values():
    for value in ("", "0", "off", "disabled", "none", None):
        assert resolve_stage(value) is KnowledgeStage.DISABLED
    assert resolve_stage("shadow") is KnowledgeStage.SHADOW
    assert resolve_stage("canary") is KnowledgeStage.RECOVERY
    assert resolve_stage("graph-wide") is KnowledgeStage.GRAPH
    assert resolve_stage("repo") is KnowledgeStage.CANONICAL
    assert resolve_stage("3") is KnowledgeStage.GRAPH
    assert resolve_stage(KnowledgeStage.RECOVERY) is KnowledgeStage.RECOVERY


def test_resolve_stage_fails_closed_on_unknown_values():
    # A typo, a bool, or an out-of-range int must never enable anything.
    assert resolve_stage("shadows") is KnowledgeStage.DISABLED
    assert resolve_stage(True) is KnowledgeStage.DISABLED
    assert resolve_stage(99) is KnowledgeStage.DISABLED
    assert resolve_stage(-1) is KnowledgeStage.DISABLED


def test_stage_from_env_reads_only_the_stage_variable():
    assert stage_from_env({}) is KnowledgeStage.DISABLED
    assert stage_from_env({STAGE_ENV: "recovery"}) is KnowledgeStage.RECOVERY
    assert stage_from_env({STAGE_ENV: "  Shadow  "}) is KnowledgeStage.SHADOW
    assert stage_from_env({"OTHER": "canonical"}) is KnowledgeStage.DISABLED


def test_enable_predicates_are_monotonic():
    expected = {
        KnowledgeStage.DISABLED: (False, False, False, False),
        KnowledgeStage.SHADOW: (True, False, False, False),
        KnowledgeStage.RECOVERY: (True, True, False, False),
        KnowledgeStage.GRAPH: (True, True, True, False),
        KnowledgeStage.CANONICAL: (True, True, True, True),
    }
    for stage, (capture, recovery, graph, canonical) in expected.items():
        assert capture_enabled(stage) is capture
        assert recovery_enabled(stage) is recovery
        assert graph_sharing_enabled(stage) is graph
        assert canonical_enabled(stage) is canonical


def test_describe_stage_is_log_safe():
    description = describe_stage(KnowledgeStage.RECOVERY)
    assert description == {
        "stage": "recovery",
        "capture": True,
        "recovery": True,
        "graph": False,
        "canonical": False,
    }
    # Values are primitives only -- safe to put in an event payload.
    assert all(
        isinstance(value, (str, bool)) for value in describe_stage("canonical").values()
    )


def test_recovery_budget_defaults_and_clamps():
    assert recovery_budget_tokens({}) == DEFAULT_RECOVERY_BUDGET_TOKENS
    assert recovery_budget_tokens({RECOVERY_BUDGET_ENV: "900"}) == 900
    # Below/above the clamp window is bounded, not accepted as-is.
    assert recovery_budget_tokens({RECOVERY_BUDGET_ENV: "10"}) == 128
    assert recovery_budget_tokens({RECOVERY_BUDGET_ENV: "999999"}) == 2000
    # Garbage falls back to the default rather than raising.
    assert recovery_budget_tokens({RECOVERY_BUDGET_ENV: "lots"}) == (
        DEFAULT_RECOVERY_BUDGET_TOKENS
    )


def test_chain_rounds_defaults_and_clamps():
    assert chain_max_rounds({}) == DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS
    assert chain_max_rounds({ATTEMPT_CHAIN_ROUNDS_ENV: "5"}) == 5
    assert chain_max_rounds({ATTEMPT_CHAIN_ROUNDS_ENV: "0"}) == 1
    assert chain_max_rounds({ATTEMPT_CHAIN_ROUNDS_ENV: "1000"}) == 100


def test_patch_capture_ceilings_default_and_clamp():
    assert max_patch_files({}) == DEFAULT_MAX_PATCH_FILES
    assert max_patch_files({MAX_PATCH_FILES_ENV: "3"}) == 3
    assert max_patch_files({MAX_PATCH_FILES_ENV: "0"}) == 1
    assert max_patch_files({MAX_PATCH_FILES_ENV: "999"}) == 200

    assert max_patch_diff_chars({}) == DEFAULT_MAX_PATCH_DIFF_CHARS
    assert max_patch_diff_chars({MAX_PATCH_DIFF_CHARS_ENV: "1200"}) == 1200
    assert max_patch_diff_chars({MAX_PATCH_DIFF_CHARS_ENV: "1"}) == 500
    assert max_patch_diff_chars({MAX_PATCH_DIFF_CHARS_ENV: "9999999"}) == 200_000


def test_defaults_come_from_the_v2_plan():
    # The plan names 300-600 tokens for retry recovery; 600 is the starting
    # ceiling, not an invented number.
    assert DEFAULT_RECOVERY_BUDGET_TOKENS == 600
