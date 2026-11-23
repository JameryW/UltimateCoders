"""Tests for V22 ledger item #91 (ParallACT/MA-Mesh)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.parallact_mesh import (
    AgentCapability,
    MeshMode,
    ParallactError,
    ParallactMesh,
    WriteScope,
)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC_PARALLACT_MESH", raising=False)
    from ultimate_coders.agent.parallact_mesh import parallact_mesh_enabled

    assert parallact_mesh_enabled() is False


def test_symbol_and_path_conflicts_are_detected() -> None:
    mesh = ParallactMesh()
    mesh.register_scope(WriteScope("a", frozenset({"foo"}), frozenset({"src/a.py"})))
    mesh.register_scope(WriteScope("b", frozenset({"foo"}), frozenset({"src/a.py"})))
    conflicts = mesh.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].overlaps == ("symbol:foo", "path:src/a.py")


def test_no_conflict_and_no_benefit_is_serial() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder"))
    mesh.register_capability(AgentCapability("b", "reviewer"))
    plan = mesh.plan()
    assert plan.mode is MeshMode.SERIAL
    assert "no evidence" in plan.reason


def test_evidence_partition_earns_parallel() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder", evidence_partitioned=True))
    mesh.register_capability(AgentCapability("b", "reviewer"))
    plan = mesh.plan()
    assert plan.mode is MeshMode.PARALLEL
    assert plan.parallel


def test_external_verification_earns_parallel() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder", external_verified=True))
    assert mesh.has_parallel_benefit() is True


def test_conflict_forces_serial_without_failover() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder", evidence_partitioned=True))
    mesh.register_capability(AgentCapability("b", "coder", evidence_partitioned=True))
    mesh.register_scope(WriteScope("a", frozenset({"foo"})))
    mesh.register_scope(WriteScope("b", frozenset({"foo"})))
    plan = mesh.plan(failed_agents=("a",))
    assert plan.mode is MeshMode.SERIAL
    assert plan.conflicts


def test_role_failover_assigns_healthy_peer() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder", evidence_partitioned=True))
    mesh.register_capability(AgentCapability("b", "coder", evidence_partitioned=True))
    plan = mesh.plan(failed_agents=("a",))
    assert plan.mode is MeshMode.PARALLEL
    assert plan.assignments == (type(plan.assignments[0])("coder", "b", "a"),)


def test_blocked_when_role_has_no_healthy_peer() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder", evidence_partitioned=True))
    plan = mesh.plan(failed_agents=("a",))
    assert plan.mode is MeshMode.BLOCKED
    assert "coder" in plan.reason


def test_empty_mesh_is_blocked() -> None:
    plan = ParallactMesh().plan()
    assert plan.mode is MeshMode.BLOCKED


def test_duplicate_capability_is_rejected() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder"))
    with pytest.raises(ParallactError):
        mesh.register_capability(AgentCapability("a", "reviewer"))


def test_plan_serialises_to_dict() -> None:
    mesh = ParallactMesh()
    mesh.register_capability(AgentCapability("a", "coder"))
    data = mesh.plan().to_dict()
    assert data["mode"] == "serial"
    assert data["assignments"][0]["agent_id"] == "a"
