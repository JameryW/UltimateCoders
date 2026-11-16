"""Tests for the v10 ExecCritic module (opt-in, default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.exec_critic import (
    EXEC_CRITIC_ENV,
    MAX_FEEDBACK_CHARS,
    STATUS_HARD_FAIL,
    STATUS_INCONCLUSIVE_INFRA,
    STATUS_OK,
    ProtectedTestError,
    SourceRepairNode,
    TestGenNode,
    exec_critic_enabled,
)


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "test_patch": "--- a/tests/test_new.py\n+++ b/tests/test_new.py\n+def test_new(): ...\n",
        "test_command": "python -m pytest tests/test_new.py",
        "test_contract": {"checks": ["returns 400 on missing token"]},
        "base_clean_fail": True,
        "hidden": True,
        "frozen": True,
    }
    candidate.update(overrides)
    return candidate


def test_generate_valid_artifact() -> None:
    node = TestGenNode()
    artifact = node.generate("task-1", {"base_clean_fail": True}, _candidate())
    assert artifact.status == STATUS_OK
    assert artifact.ok is True
    assert artifact.hidden is True
    assert artifact.frozen is True
    assert artifact.base_clean_fail is True
    assert artifact.test_contract["checks"]


def test_generation_is_gold_free() -> None:
    node = TestGenNode()
    artifact = node.generate(
        "task-1",
        {"base_clean_fail": True},
        _candidate(gold_patch="--- a/src/solution.py\n+++ b/src/solution.py\n+fixed\n"),
    )
    assert artifact.status == STATUS_HARD_FAIL
    assert "gold-free" in artifact.reason


def test_clean_base_must_fail() -> None:
    node = TestGenNode()
    artifact = node.generate("task-1", {"base_clean_fail": False}, _candidate())
    assert artifact.status == STATUS_HARD_FAIL
    assert "clean base" in artifact.reason


def test_test_patch_and_contract_required() -> None:
    node = TestGenNode()
    assert node.generate("t", {"base_clean_fail": True}, _candidate(test_patch="")).status == (
        STATUS_HARD_FAIL
    )
    assert node.generate("t", {"base_clean_fail": True}, _candidate(test_contract="")).status == (
        STATUS_HARD_FAIL
    )
    assert (
        node.generate("t", {"base_clean_fail": True}, _candidate(test_contract="{not json}")).status
        == STATUS_HARD_FAIL
    )


def test_hidden_and_frozen_required() -> None:
    node = TestGenNode()
    assert node.generate("t", {"base_clean_fail": True}, _candidate(hidden=False)).status == (
        STATUS_HARD_FAIL
    )
    assert node.generate("t", {"base_clean_fail": True}, _candidate(frozen=False)).status == (
        STATUS_HARD_FAIL
    )


def test_infra_failure_is_inconclusive_not_hard_fail() -> None:
    def exploding_runner(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("docker daemon unavailable")

    node = TestGenNode(base_runner=exploding_runner)
    artifact = node.generate("t", {"base_clean_fail": True}, _candidate())
    assert artifact.status == STATUS_INCONCLUSIVE_INFRA
    assert "infrastructure" in artifact.reason


def test_runner_can_prove_clean_base_fail() -> None:
    node = TestGenNode(base_runner=lambda _tree, _patch, _command: True)
    artifact = node.generate("t", None, _candidate(base_clean_fail=False))
    assert artifact.status == STATUS_OK


def test_source_repair_rejects_protected_tests() -> None:
    node = SourceRepairNode()
    patch = (
        "--- a/tests/python/test_thing.py\n+++ b/tests/python/test_thing.py\n"
        "@@ -1 +1 @@\n-assert 1 == 1\n+assert 1 == 2\n"
    )
    with pytest.raises(ProtectedTestError):
        node.repair("task-1", patch)


def test_source_repair_feedback_cap_and_cumulative_patch() -> None:
    node = SourceRepairNode()
    feedback = "f" * (MAX_FEEDBACK_CHARS + 500)
    result = node.repair(
        "task-1",
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        feedback=feedback,
        previous_patch="--- a/src/old.py\n+++ b/src/old.py\n@@ -1 +1 @@\n-x\n+y\n",
    )
    assert result.status == STATUS_OK
    assert result.feedback_chars == MAX_FEEDBACK_CHARS
    assert result.source_patch.count("--- a/") == 2


def test_source_repair_empty_patch_hard_fails() -> None:
    result = SourceRepairNode().repair("task-1", "")
    assert result.status == STATUS_HARD_FAIL
    assert result.source_patch == ""


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_exec_critic_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(EXEC_CRITIC_ENV, value)
    assert exec_critic_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_exec_critic_flag_falsey(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(EXEC_CRITIC_ENV, value)
    assert exec_critic_enabled() is False
