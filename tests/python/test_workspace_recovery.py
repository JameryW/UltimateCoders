"""Operator workspace recovery: every guard, and what it releases (T60).

`RemoteJobs.recover_workspace` is the emergency path for a lease whose worker died:
compare-and-set on the lease, then host identity, then local process birth, then the
remote terminal states of every experiment in the worktree — and only then does it mark
the lease reconciled and release the GPU reservations it proved nobody is using.

None of that was tested. These tests pin each refusal (so a loosened guard is visible), the
happy path, and the release bookkeeping (a reservation that cannot be released must not
leak the rest of them).
"""

from __future__ import annotations

import socket

import pytest
from ultimate_coders.inference import reconcile as reconcile_module
from ultimate_coders.inference.reconcile import RemoteJobs
from ultimate_coders.runtime_state import RecordConflictError, RuntimeState

WORKTREE = "/workspaces/wt-1"


@pytest.fixture
def state(tmp_path, monkeypatch) -> RuntimeState:
    monkeypatch.setenv("UC_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("UC_DATABASE_URL", raising=False)
    return RuntimeState()


def _lease(state: RuntimeState, key: str = "lease-1", **overrides) -> int:
    record = {
        "host": socket.gethostname(),
        "handle": {"worktree_path": WORKTREE, "status": "active"},
        **overrides,
    }
    state.mutate("workspace_leases", key, lambda old: record)
    return state.versioned_get("workspace_leases", key).version


def _experiment(state: RuntimeState, key: str, reservations: list[str]) -> None:
    state.mutate(
        "experiments",
        key,
        lambda old: {"workspace": WORKTREE, "gpu_reservations": list(reservations)},
    )


def _remote_job(state: RuntimeState, key: str, status: str) -> None:
    state.mutate("remote_jobs", key, lambda old: {"state": status})


def _reserve(state: RuntimeState, resource_id: str, holder: str) -> None:
    state.mutate(
        "resource_budgets",
        resource_id,
        lambda old: {
            "capacity": 1,
            "holders": {holder: {"acquired_at": 1.0}},
            "queue": [],
            "state": "occupied",
        },
    )


def _holders(state: RuntimeState, resource_id: str) -> dict:
    return (state.get("resource_budgets", resource_id) or {}).get("holders", {})


def test_a_lease_on_another_host_is_refused(state):
    version = _lease(state, host="some-other-host")
    with pytest.raises(ValueError, match="owning worker host"):
        RemoteJobs(state, "http://metainfer.test").recover_workspace(
            "lease-1", version, actor="ops:alice"
        )
    assert state.get("workspace_leases", "lease-1")["host"] == "some-other-host"


def test_a_live_local_owner_is_refused(state, monkeypatch):
    version = _lease(state, pid=4321, process_identity="birth-1")
    monkeypatch.setattr(reconcile_module, "process_matches", lambda pid, birth: True)

    with pytest.raises(ValueError, match="live local owner"):
        RemoteJobs(state, "http://metainfer.test").recover_workspace(
            "lease-1", version, actor="ops:alice"
        )
    assert state.get("workspace_leases", "lease-1")["handle"]["status"] == "active"


def test_an_unresolved_remote_job_blocks_recovery(state, monkeypatch):
    version = _lease(state)
    _experiment(state, "exp-1", [])
    _remote_job(state, "exp-1:op-1", "running")
    monkeypatch.setattr(reconcile_module, "process_matches", lambda pid, birth: False)

    with pytest.raises(ValueError, match="unresolved remote execution"):
        RemoteJobs(state, "http://metainfer.test").recover_workspace(
            "lease-1", version, actor="ops:alice"
        )


def test_a_stale_version_is_refused(state):
    _lease(state)
    with pytest.raises(RecordConflictError):
        RemoteJobs(state, "http://metainfer.test").recover_workspace(
            "lease-1", 999, actor="ops:alice"
        )


def test_a_clean_recovery_marks_the_lease_and_releases_its_reservations(state, monkeypatch):
    version = _lease(state, pid=4321, process_identity="birth-1")
    monkeypatch.setattr(reconcile_module, "process_matches", lambda pid, birth: False)
    _experiment(state, "exp-1", ["gpu:u1", "gpu:u2"])
    _remote_job(state, "exp-1:op-1", "completed")
    _remote_job(state, "exp-1:op-2", "stopped")
    _reserve(state, "gpu:u1", "exp-1")
    _reserve(state, "gpu:u2", "exp-1")

    result = RemoteJobs(state, "http://metainfer.test").recover_workspace(
        "lease-1", version, actor="ops:alice"
    )

    assert result["status"] == "reconciled"
    assert result["handle"]["status"] == "reconciled"
    assert result["released_reservations"] == ["gpu:u1", "gpu:u2"]
    assert result["unreleased_reservations"] == []
    assert _holders(state, "gpu:u1") == {}
    assert _holders(state, "gpu:u2") == {}


def test_one_unreleasable_reservation_does_not_leak_the_others(state, monkeypatch):
    version = _lease(state)
    monkeypatch.setattr(reconcile_module, "process_matches", lambda pid, birth: False)
    _experiment(state, "exp-1", ["gpu:bad", "gpu:good"])
    _reserve(state, "gpu:bad", "exp-1")
    _reserve(state, "gpu:good", "exp-1")

    real_budget = reconcile_module.ResourceBudget

    class FlakyBudget:
        def __init__(self, inner_state, resource_id, *args, **kwargs):
            self.inner = real_budget(inner_state, resource_id)

        def release(self, operation_id: str) -> None:
            if self.inner.resource_id == "gpu:bad":
                raise RuntimeError("budget record unreadable")
            self.inner.release(operation_id)

    monkeypatch.setattr(reconcile_module, "ResourceBudget", FlakyBudget)

    result = RemoteJobs(state, "http://metainfer.test").recover_workspace(
        "lease-1", version, actor="ops:alice"
    )

    assert result["released_reservations"] == ["gpu:good"]
    assert result["unreleased_reservations"] == ["gpu:bad"]
    assert _holders(state, "gpu:good") == {}, "the other reservation was still released"
    assert _holders(state, "gpu:bad") == {"exp-1": {"acquired_at": 1.0}}
