"""Worker-side BoundaryContract (#7) tests.

The Rust contract is authoritative; these tests cover the Python read model and
the opt-in worker gate that consumes a PerceptionPass spec.
"""

from __future__ import annotations

from types import SimpleNamespace

from ultimate_coders.agent.boundary_contract import (
    BoundaryContract,
    BoundaryContractError,
    BoundaryRole,
    boundary_contract_enabled,
    boundary_contract_enforce_enabled,
)
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.types import FileChange
from ultimate_coders.agent.worker import Worker


def test_unlisted_paths_are_inspect_only() -> None:
    contract = BoundaryContract.from_entries([])

    assert contract.role_for("anything.py") is BoundaryRole.INSPECT_ONLY
    assert contract.editable_paths() == ()


def test_contract_from_perception_spec_marks_targets_editable() -> None:
    spec = SimpleNamespace(
        target_files=[
            SimpleNamespace(path="pkg/calc.py", role="module"),
            SimpleNamespace(path="calc.py", role="module"),
        ],
        dependency_closure=["pkg/helpers.py", "tests/test_calc.py"],
    )

    contract = BoundaryContract.from_implementation_spec(spec, file_constraints=["./api.py"])

    assert contract.role_for("pkg/calc.py") is BoundaryRole.EDITABLE
    assert contract.role_for("calc.py") is BoundaryRole.EDITABLE
    assert contract.role_for("api.py") is BoundaryRole.EDITABLE
    assert contract.role_for("pkg/helpers.py") is BoundaryRole.INSPECT_ONLY
    assert contract.role_for("tests/test_calc.py") is BoundaryRole.INSPECT_ONLY
    violations = contract.violations(["calc.py", "pkg/helpers.py"])
    assert [item.path for item in violations] == ["pkg/helpers.py"]


def test_contract_rejects_empty_and_duplicate_conflicting_paths() -> None:
    try:
        BoundaryContract(entries={"": BoundaryRole.EDITABLE})
    except BoundaryContractError as exc:
        assert "empty path" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("empty path must be rejected")

    # A mapping cannot contain duplicate keys, but conflicting roles through
    # different spellings are still rejected deterministically.
    try:
        BoundaryContract(entries={"a.py": BoundaryRole.EDITABLE, "./a.py": BoundaryRole.PROTECTED})
    except BoundaryContractError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("conflicting duplicate must be rejected")


def test_perception_pass_builds_boundary_contract(monkeypatch) -> None:
    from ultimate_coders.agent import planner

    monkeypatch.setenv("UC_BOUNDARY_CONTRACT", "1")
    monkeypatch.delenv("UC_PERCEPTION_PASS", raising=False)
    spec = SimpleNamespace(
        target_files=[SimpleNamespace(path="calc.py", role="module")],
        dependency_closure=["helpers.py"],
    )
    monkeypatch.setattr(planner.PerceptionPass, "compile", lambda *a, **k: spec)

    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="sub-1",
        parent_id="task-1",
        description="change calc.py",
        file_constraints=["api.py"],
    )

    assert worker._apply_perception_pass(subtask, "base context", "repo") == "base context"
    assert subtask._boundary_contract.role_for("calc.py") is BoundaryRole.EDITABLE
    assert subtask._boundary_contract.role_for("api.py") is BoundaryRole.EDITABLE
    assert subtask._boundary_contract.role_for("helpers.py") is BoundaryRole.INSPECT_ONLY


def test_worker_gate_reports_edits_outside_contract(monkeypatch) -> None:
    monkeypatch.setenv("UC_BOUNDARY_CONTRACT", "1")
    monkeypatch.setenv("UC_BOUNDARY_CONTRACT_ENFORCE", "1")
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="sub-1",
        _boundary_contract=BoundaryContract(
            entries={
                "calc.py": BoundaryRole.EDITABLE,
                "helpers.py": BoundaryRole.INSPECT_ONLY,
            }
        ),
    )
    output = AgentOutput(
        summary="changed two files",
        file_changes=[FileChange(file_path="calc.py"), FileChange(file_path="helpers.py")],
    )

    event, error = worker._verify_output_boundary(subtask, output)

    assert event is not None
    assert event["boundary_violations"] == [
        {"path": "helpers.py", "role": "inspect_only", "reason": ""}
    ]
    assert event["boundary_enforced"] is True
    assert error is not None and "helpers.py:inspect_only" in error


def test_worker_gate_is_diagnostic_without_enforce_flag(monkeypatch) -> None:
    monkeypatch.setenv("UC_BOUNDARY_CONTRACT", "1")
    monkeypatch.delenv("UC_BOUNDARY_CONTRACT_ENFORCE", raising=False)
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(
        id="sub-1",
        _boundary_contract=BoundaryContract(
            entries={"calc.py": BoundaryRole.EDITABLE}
        ),
    )
    output = AgentOutput(file_changes=[FileChange(file_path="other.py")])

    event, error = worker._verify_output_boundary(subtask, output)

    assert error is None
    assert event is not None
    assert event["boundary_enforced"] is False
    assert event["boundary_violations"][0]["path"] == "other.py"


def test_worker_gate_noops_without_a_contract(monkeypatch) -> None:
    monkeypatch.setenv("UC_BOUNDARY_CONTRACT", "1")
    worker = object.__new__(Worker)
    subtask = SimpleNamespace(id="sub-1")

    assert worker._verify_output_boundary(subtask, AgentOutput()) == (None, None)


def test_feature_flags_default_off(monkeypatch) -> None:
    monkeypatch.delenv("UC_BOUNDARY_CONTRACT", raising=False)
    monkeypatch.delenv("UC_BOUNDARY_CONTRACT_ENFORCE", raising=False)

    assert boundary_contract_enabled() is False
    assert boundary_contract_enforce_enabled() is False
