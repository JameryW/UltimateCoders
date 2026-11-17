"""Worker boundary tests for the opt-in V10 PerceptionPass and anti-pattern gates."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from ultimate_coders.agent.context_compiler import ContextItem, ContextManifest, Tier
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange
from ultimate_coders.agent.worker import Worker, _parse_sandbox_line


def test_perception_pass_appends_read_only_spec(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def build(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        description="Add a builder in `pkg/core.py` that returns a value plus one.",
        file_constraints=["pkg/core.py"],
    )

    monkeypatch.delenv("UC_PERCEPTION_PASS", raising=False)
    assert worker._apply_perception_pass(subtask, "base context", str(tmp_path)) == "base context"

    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    rendered = worker._apply_perception_pass(subtask, "base context", str(tmp_path))
    assert "base context" in rendered
    assert "## Implementation spec (PerceptionPass)" in rendered
    assert "`pkg/core.py`" in rendered


def test_perception_pass_failure_keeps_original_context(tmp_path: Path, monkeypatch) -> None:
    from ultimate_coders.agent import planner

    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        description="Touch `missing.py`.",
        file_constraints=["missing.py"],
    )
    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    monkeypatch.setattr(
        planner.PerceptionPass,
        "compile",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert worker._apply_perception_pass(subtask, "unchanged", str(tmp_path)) == "unchanged"


def test_plan_critic_flags_missing_target_after_perception(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def build(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        description="Add a builder in `pkg/core.py` and `missing.py`.",
        file_constraints=["pkg/core.py", "missing.py"],
    )
    monkeypatch.setenv("UC_PERCEPTION_PASS", "1")
    monkeypatch.setenv("UC_PLANNER_CRITIC", "1")

    rendered = worker._apply_perception_pass(subtask, "base context", str(tmp_path))
    assert "### Plan critic: not approved" in rendered
    assert "Collect concrete evidence for `missing.py`" in rendered


def test_runtime_diagnosis_reads_failed_command_output(monkeypatch) -> None:
    worker = object.__new__(Worker)
    output = AgentOutput(
        summary="test failed",
        success=False,
        test_output=(
            "_____________________________ test_calculate _____________________________\n"
            "test_probe.py:17: in test_calculate\n"
            "    assert result == expected\n"
            "E   assert 3 == 4\n"
            "        expected   = 4\n"
            "        result     = 3\n"
        ),
    )

    monkeypatch.delenv("UC_SWE_DOCTOR", raising=False)
    assert worker._runtime_diagnosis(output) is None

    monkeypatch.setenv("UC_SWE_DOCTOR", "1")
    payload = worker._runtime_diagnosis(output)
    assert payload is not None
    assert payload["phase"] == "runtime diagnosis"
    assert payload["divergence_frame"] == "test_probe.py:17: in test_calculate"
    assert any(item["name"] == "result" for item in payload["snapshots"])
    assert "[snapshots]" not in payload["runtime_summary"]


def test_runtime_diagnosis_skips_successful_or_empty_runs(monkeypatch) -> None:
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_SWE_DOCTOR", "1")
    assert worker._runtime_diagnosis(AgentOutput(success=True, test_output="E assert 1")) is None
    assert worker._runtime_diagnosis(AgentOutput(success=False, test_output="")) is None


def test_drift_hints_use_declared_file_constraints() -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(file_constraints=["src/a.py", "src/b.py"])
    assert worker._drift_hints(subtask) == {
        "_uc_drift_boundary": "src/a.py,src/b.py",
        "_uc_drift_required_files": "src/a.py,src/b.py",
    }

    subtask = SimpleNamespace(file_constraints=[])
    assert worker._drift_hints(subtask) == {}


def test_drift_advisor_line_becomes_task_progress() -> None:
    parsed = _parse_sandbox_line(
        json.dumps(
            {
                "event": "drift_advisor",
                "signals": ["edit_failures"],
                "prompt": "Stop editing and re-read.",
            }
        )
    )
    assert parsed is not None
    event_type, data = parsed
    assert event_type == "subtask_progress"
    assert data["phase"] == "drift advisor"
    assert data["drift_signals"] == ["edit_failures"]
    assert data["drift_prompt"] == "Stop editing and re-read."


def test_radio_hints_are_opt_in_and_task_scoped(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(id="node-1", parent_id="graph-1")

    monkeypatch.delenv("UC_AGENT_RADIO", raising=False)
    assert worker._radio_hints(subtask) == {}

    monkeypatch.setenv("UC_AGENT_RADIO", "1")
    assert worker._radio_hints(subtask) == {
        "_uc_radio_graph_id": "graph-1",
        "_uc_radio_node_id": "node-1",
    }

    orphan = SimpleNamespace(id="node-2", parent_id="")
    assert worker._radio_hints(orphan) == {
        "_uc_radio_graph_id": "node-2",
        "_uc_radio_node_id": "node-2",
    }


def test_radio_events_become_task_progress() -> None:
    post = _parse_sandbox_line(
        json.dumps(
            {
                "event": "radio_post",
                "message": {
                    "message_id": "m-1",
                    "kind": "worklog",
                    "node_id": "node-1",
                    "text": "working",
                },
            }
        )
    )
    assert post is not None
    event_type, data = post
    assert event_type == "subtask_progress"
    assert data["phase"] == "agent radio"
    assert data["radio_message_id"] == "m-1"
    assert data["radio_text"] == "working"

    update = _parse_sandbox_line(
        json.dumps({"event": "teammate_radio_update", "text": "Radio updates:\n- [mention] n2: hi"})
    )
    assert update is not None
    event_type, data = update
    assert event_type == "subtask_progress"
    assert data["phase"] == "teammate radio update"
    assert "mention" in data["radio_update"]


def test_prefix_context_assembly_projects_manifest_slots(monkeypatch) -> None:
    worker = object.__new__(Worker)
    monkeypatch.setattr(worker, "_context_budget_tokens", lambda: 2000)
    manifest = ContextManifest(
        items=[
            ContextItem("policy-1", Tier.POLICY, "policy text", stable=True),
            ContextItem("evidence-1", Tier.EVIDENCE, "evidence text"),
            ContextItem("trace-1", Tier.TRACE, "trace text"),
            ContextItem("state-1", Tier.STATE, "state text"),
        ],
        budget_tokens=2000,
    )

    monkeypatch.delenv("UC_PREFIX_CONTEXT_ASSEMBLY", raising=False)
    assert worker._assembled_context_block(manifest) is None

    monkeypatch.setenv("UC_PREFIX_CONTEXT_ASSEMBLY", "1")
    rendered, summary = worker._assembled_context_block(manifest)
    assert summary["slots"] == [
        "system_schema",
        "declarative",
        "procedural",
        "observation",
    ]
    assert summary["prefix_cache_hint"]
    assert summary["stable_tokens"] > 0
    assert rendered.index("policy text") < rendered.index("evidence text")
    assert rendered.index("evidence text") < rendered.index("trace text")
    assert rendered.index("trace text") < rendered.index("state text")
    assert all(
        text in rendered
        for text in ("policy text", "evidence text", "trace text", "state text")
    )


async def test_dockerless_verifier_reports_changed_signature(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "foo.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    source.write_text("def add(a, b, c):\n    return a + b + c\n", encoding="utf-8")
    output = AgentOutput(file_changes=[FileChange(file_path="foo.py")])
    worker = object.__new__(Worker)

    monkeypatch.delenv("UC_DOCKERLESS_VERIFIER", raising=False)
    assert await worker._verify_output_dockerless(str(tmp_path), output) is None

    monkeypatch.setenv("UC_DOCKERLESS_VERIFIER", "1")
    report = await worker._verify_output_dockerless(str(tmp_path), output)
    assert report is not None
    assert report.passed is False
    assert report.score == 67
    assert any("signature mismatch" in item for item in report.missing_evidence)


async def test_retrace_verifier_reports_diagnostic(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    source.write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def multiply(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    output = AgentOutput(
        summary="Added multiply and kept add.",
        success=True,
        file_changes=[FileChange(file_path="calc.py")],
        commands_run=["pytest -q"],
    )
    subtask = SimpleNamespace(
        id="subtask-1",
        description="Add multiply(a, b) that must return the product.",
    )
    worker = object.__new__(Worker)

    monkeypatch.delenv("UC_RETRACE_VERIFIER", raising=False)
    assert await worker._verify_output_retrace(subtask, str(tmp_path), output) is None

    monkeypatch.setenv("UC_RETRACE_VERIFIER", "1")
    report = await worker._verify_output_retrace(subtask, str(tmp_path), output)
    assert report is not None
    assert report.status in {"agree", "mismatch", "contradictory"}
    assert isinstance(report.repair_instruction, str)


def test_exec_critic_artifact_gate_is_opt_in_and_enforced(monkeypatch) -> None:
    valid = {
        "test_patch": (
            "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
            "--- a/tests/test_calc.py\n"
            "+++ b/tests/test_calc.py\n"
            "@@ -1 +1 @@\n"
            "-assert add(1, 2) == 3\n"
            "+assert add(1, 2) == 4\n"
        ),
        "test_command": "pytest -q tests/test_calc.py",
        "test_contract": {"target": "calc.py"},
        "base_clean_fail": True,
        "hidden": True,
        "frozen": True,
    }
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        agent_config={"test_artifact": {**valid, "enforce": True}},
    )

    monkeypatch.delenv("UC_EXEC_CRITIC", raising=False)
    assert worker._exec_critic_preflight(subtask) == (None, None)

    monkeypatch.setenv("UC_EXEC_CRITIC", "1")
    event, error = worker._exec_critic_preflight(subtask)
    assert error is None
    assert event is not None
    assert event["exec_critic_status"] == "ok"
    assert event["exec_critic_passed"] is True
    assert subtask._exec_critic_artifact is not None


def test_exec_critic_artifact_rejects_invalid_artifact_when_enforced(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        agent_config={"test_artifact": {"test_patch": "", "enforce": True}},
    )
    monkeypatch.setenv("UC_EXEC_CRITIC", "1")

    event, error = worker._exec_critic_preflight(subtask)
    assert event is not None
    assert event["exec_critic_status"] == "hard_fail"
    assert error is not None
    assert "test artifact rejected" in error.lower()


async def test_exec_critic_protects_declared_tests(tmp_path: Path, monkeypatch) -> None:
    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    test_file.write_text("def test_add():\n    pass\n", encoding="utf-8")
    output = AgentOutput(file_changes=[FileChange(file_path="tests/test_calc.py")])
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_EXEC_CRITIC", "1")
    subtask = SimpleNamespace(
        id="subtask-1",
        agent_config={},
        _exec_critic_artifact=SimpleNamespace(
            test_patch=(
                "diff --git a/tests/test_calc.py b/tests/test_calc.py\n"
                "--- a/tests/test_calc.py\n"
                "+++ b/tests/test_calc.py\n"
            )
        ),
        _exec_critic_enforce=True,
    )

    event, error = await worker._verify_output_exec_critic(subtask, str(tmp_path), output)
    assert event is not None
    assert event["exec_critic_passed"] is False
    assert event["exec_critic_status"] == "hard_fail"
    assert error is not None
    assert "may not modify protected test files" in error


async def test_exec_critic_allows_source_only_patch(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    source.write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
    output = AgentOutput(file_changes=[FileChange(file_path="calc.py")])
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_EXEC_CRITIC", "1")
    subtask = SimpleNamespace(
        id="subtask-1",
        agent_config={},
        _exec_critic_artifact=SimpleNamespace(test_patch=""),
        _exec_critic_enforce=True,
    )

    event, error = await worker._verify_output_exec_critic(subtask, str(tmp_path), output)
    assert event is not None
    assert event["exec_critic_passed"] is True
    assert event["exec_critic_status"] == "ok"
    assert error is None


async def test_anti_pattern_gate_rejects_deleted_assertion(tmp_path: Path, monkeypatch) -> None:
    test_file = tmp_path / "tests" / "test_calc.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "def test_add():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)

    test_file.write_text("def test_add():\n    pass\n", encoding="utf-8")
    output = AgentOutput(
        file_changes=[FileChange(file_path="tests/test_calc.py")],
        commands_run=["pytest -q"],
    )
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_ANTI_PATTERN_GATE", "1")

    report = await worker._verify_output_anti_patterns(str(tmp_path), output)
    assert report is not None
    assert not report.passed
    assert any(item.kind == "test_file_tampering" for item in report.errors)


def _mandate_subtask(**overrides):
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        required_capabilities=["code"],
        file_constraints=["src/app.py"],
        dispatch_retry_count=0,
        agent_config={},
    )
    for key, value in overrides.items():
        setattr(subtask, key, value)
    return subtask


def _valid_mandate(**overrides):
    mandate = {
        "mandate_id": "mandate-1",
        "subject": "task-1",
        "capabilities": ["code"],
        "resources": ["src/app.py"],
        "max_attempts": 2,
        "budget_tokens": 1000,
        "expires_at": 0.0,
    }
    mandate.update(overrides)
    return mandate


def test_mandate_gate_is_opt_in(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(agent_config={"mandate": _valid_mandate()})
    monkeypatch.delenv("UC_MANDATE_ENVELOPE", raising=False)

    assert worker._apply_mandate(subtask) == (None, None)


def test_mandate_gate_accepts_a_valid_envelope(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(agent_config={"mandate": _valid_mandate()})
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", "1")

    event, error = worker._apply_mandate(subtask)
    assert error is None
    assert event is not None
    assert event["mandate_id"] == "mandate-1"
    assert len(event["mandate_digest"]) == 64
    assert event["mandate_violations"] == []
    assert subtask._mandate.mandate_id == "mandate-1"


def test_mandate_gate_reports_capabilities_resources_and_attempt_ceiling(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(
        dispatch_retry_count=1,
        agent_config={
            "mandate": _valid_mandate(
                capabilities=["review"],
                resources=["other.py"],
                max_attempts=1,
            )
        },
    )
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", "1")

    event, error = worker._apply_mandate(subtask)
    assert error is None
    assert event is not None
    violations = event["mandate_violations"]
    assert any("capabilities" in item for item in violations)
    assert any("resources" in item for item in violations)
    assert any("max_attempts" in item for item in violations)


def test_mandate_gate_enforces_violations(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(
        agent_config={
            "mandate": _valid_mandate(
                capabilities=[],
                resources=[],
                enforce=True,
            )
        },
    )
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", "1")

    event, error = worker._apply_mandate(subtask)
    assert event is not None
    assert event["mandate_enforced"] is True
    assert error is not None
    assert error.startswith("Mandate envelope rejected:")


def test_mandate_gate_rejects_expired_and_foreign_subjects(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(
        agent_config={
            "mandate": _valid_mandate(
                subject="another-task",
                expires_at=1.0,
                enforce=True,
            )
        },
    )
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", "1")

    event, error = worker._apply_mandate(subtask)
    assert event is not None
    assert event["mandate_expired"] is True
    assert error is not None
    assert "does not match this task" in error
    assert "expired" in error


def test_mandate_gate_always_rejects_malformed_envelopes(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = _mandate_subtask(
        agent_config={"mandate": _valid_mandate(expires_at="not-a-number")},
    )
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", "1")

    event, error = worker._apply_mandate(subtask)
    assert event is not None
    assert "mandate_error" in event
    assert error is not None
    assert error.startswith("Mandate envelope rejected:")


def test_acceptance_scores_report_a_declared_gdpo_group(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        agent_config={
            "acceptance_scores": [
                {"pass_rate": 0.0, "f2p_rate": 0.0, "p2p_rate": 0.0},
                {"pass_rate": 1.0, "f2p_rate": 1.0, "p2p_rate": 1.0},
            ]
        }
    )

    monkeypatch.delenv("UC_GDPO_SCORER", raising=False)
    assert worker._acceptance_scores_event(subtask) is None

    monkeypatch.setenv("UC_GDPO_SCORER", "1")
    event = worker._acceptance_scores_event(subtask)
    assert event is not None
    assert event["gdpo_status"] == "ok"
    assert event["gdpo_group_size"] == 2
    assert len(event["gdpo_rewards"]) == 2
    assert event["gdpo_rewards"][0] < event["gdpo_rewards"][1]


def test_acceptance_scores_require_a_group_and_valid_entries(monkeypatch) -> None:
    worker = object.__new__(Worker)
    monkeypatch.setenv("UC_GDPO_SCORER", "1")

    single = SimpleNamespace(
        agent_config={"acceptance_scores": [{"pass_rate": 1.0}]}
    )
    event = worker._acceptance_scores_event(single)
    assert event is not None
    assert event["gdpo_status"] == "insufficient_group"

    malformed = SimpleNamespace(agent_config={"acceptance_scores": [{"pass_rate": "x"}, {}]})
    event = worker._acceptance_scores_event(malformed)
    assert event is not None
    assert event["gdpo_status"] == "invalid"
    assert "pass_rate" in event["gdpo_reason"]


def test_skill_card_is_injected_only_after_validation(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        agent_config={
            "skill_card": {
                "skill_id": "retry-after-timeout",
                "trigger": "a command timed out",
                "procedure": "re-run with a smaller scope",
                "verification": "the narrowed command succeeds",
            },
            "skill_evidence": {"replay": True, "transfer": True, "safety": True},
        }
    )

    monkeypatch.delenv("UC_SKILL_CURRICULUM", raising=False)
    block, event = worker._apply_skill_card(subtask, "base")
    assert block == "base"
    assert event is None

    monkeypatch.setenv("UC_SKILL_CURRICULUM", "1")
    block, event = worker._apply_skill_card(subtask, "base")
    assert "### Validated skill card: retry-after-timeout" in block
    assert "Procedure: re-run with a smaller scope" in block
    assert event is not None
    assert event["skill_passed"] is True


def test_skill_card_requires_stage_evidence(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        agent_config={
            "skill_card": {
                "skill_id": "retry-after-timeout",
                "trigger": "a command timed out",
                "procedure": "re-run with a smaller scope",
                "verification": "the narrowed command succeeds",
            },
            "skill_evidence": {"schema": True},
        }
    )
    monkeypatch.setenv("UC_SKILL_CURRICULUM", "1")

    block, event = worker._apply_skill_card(subtask, "base")
    assert block == "base"
    assert event is not None
    assert event["skill_passed"] is False
    assert event["skill_failed_stage"] == "replay"


def test_test_execution_event_reports_observed_delta(monkeypatch, tmp_path: Path) -> None:
    from ultimate_coders.acceptance import test_execution

    class FakeDelta:
        def to_dict(self):
            return {
                "pass_rate": 0.75,
                "f2p_rate": 0.5,
                "p2p_rate": 1.0,
                "f2p": ["test_mod::test_fix"],
                "p2p": ["test_mod::test_stable"],
            }

    monkeypatch.setattr(test_execution, "test_execution_enabled", lambda: True)
    monkeypatch.setattr(test_execution, "run_test_delta", lambda *args, **kwargs: FakeDelta())
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        agent_config={"test_execution": {"command": ["pytest", "-q"]}},
    )

    event = asyncio.run(worker._test_execution_event(str(tmp_path), subtask))
    assert event is not None
    assert event["phase"] == "test execution"
    assert event["test_execution_status"] == "ok"
    assert event["test_execution_enforced"] is False
    assert event["pass_rate"] == 0.75
    assert event["f2p_rate"] == 0.5
    assert Worker._test_execution_error(event) is None
    result_data = Worker._test_execution_result_data(event)
    assert result_data is not None
    assert result_data["status"] == "ok"
    assert result_data["f2p"] == ["test_mod::test_fix"]
    assert result_data["f2p_tests"] == ["test_mod::test_fix"]

    monkeypatch.setattr(
        test_execution,
        "run_test_delta",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no junit")),
    )
    failed = asyncio.run(worker._test_execution_event(str(tmp_path), subtask))
    assert failed is not None
    assert failed["test_execution_status"] == "error"
    assert "no junit" in failed["test_execution_reason"]
    assert Worker._test_execution_result_data(failed) is None


def test_test_execution_is_opt_in(monkeypatch, tmp_path: Path) -> None:
    from ultimate_coders.acceptance import test_execution

    monkeypatch.setattr(test_execution, "test_execution_enabled", lambda: False)
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(agent_config={"test_execution": {"command": ["pytest"]}})
    assert asyncio.run(worker._test_execution_event(str(tmp_path), subtask)) is None


def test_test_execution_enforce_rejects_observed_regressions(monkeypatch, tmp_path: Path) -> None:
    from ultimate_coders.acceptance import test_execution

    class FakeDelta:
        def to_dict(self):
            return {
                "pass_rate": 0.5,
                "f2p_rate": 0.0,
                "p2p_rate": 0.5,
                "f2p": [],
                "p2p": ["test_mod::test_stable"],
                "broken": ["test_mod::test_was_green"],
            }

    monkeypatch.setattr(test_execution, "test_execution_enabled", lambda: True)
    monkeypatch.setattr(test_execution, "run_test_delta", lambda *args, **kwargs: FakeDelta())
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        agent_config={
            "test_execution": {
                "command": ["pytest", "-q"],
                "enforce": True,
            }
        },
    )

    event = asyncio.run(worker._test_execution_event(str(tmp_path), subtask))
    assert event is not None
    assert event["test_execution_enforced"] is True
    error = Worker._test_execution_error(event)
    assert error is not None
    assert "Observed test regression" in error
    assert "test_mod::test_was_green" in error


def test_test_execution_enforce_fails_closed_on_runner_error(monkeypatch, tmp_path: Path) -> None:
    from ultimate_coders.acceptance import test_execution

    monkeypatch.setattr(test_execution, "test_execution_enabled", lambda: True)
    monkeypatch.setattr(
        test_execution,
        "run_test_delta",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pytest missing")),
    )
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        parent_id="task-1",
        agent_config={
            "test_execution": {
                "command": ["pytest", "-q"],
                "enforce": True,
            }
        },
    )

    event = asyncio.run(worker._test_execution_event(str(tmp_path), subtask))
    assert event is not None
    assert event["test_execution_status"] == "error"
    assert event["test_execution_enforced"] is True
    error = Worker._test_execution_error(event)
    assert error is not None
    assert "Test execution gate rejected" in error
    assert "pytest missing" in error


def test_credential_preflight_consumes_a_preissued_lease(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="subtask-1",
        required_capabilities=["code"],
    )
    monkeypatch.delenv("UC_CREDENTIAL_BROKER", raising=False)
    assert worker._credential_preflight(subtask) == ({}, None, None)

    monkeypatch.setenv("UC_CREDENTIAL_BROKER", "https://broker.internal")
    monkeypatch.setenv("UC_CREDENTIAL_LEASE", "lease-token")
    env, event, error = worker._credential_preflight(subtask)
    assert error is None
    assert env == {
        "_uc_credential_lease": "lease-token",
        "_uc_credential_broker": "https://broker.internal",
    }
    assert event is not None
    assert event["credential_status"] == "leased"
    assert event["credential_lease_present"] is True
    assert "lease-token" not in json.dumps(event)


def test_credential_preflight_fails_closed_when_lease_is_missing(monkeypatch) -> None:
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(id="subtask-1", required_capabilities=["code"])
    monkeypatch.setenv("UC_CREDENTIAL_BROKER", "https://broker.internal")
    monkeypatch.delenv("UC_CREDENTIAL_LEASE", raising=False)

    env, event, error = worker._credential_preflight(subtask)
    assert env == {}
    assert error is not None and "UC_CREDENTIAL_LEASE" in error
    assert event is not None and event["credential_status"] == "invalid"
    assert event["credential_lease_present"] is False
