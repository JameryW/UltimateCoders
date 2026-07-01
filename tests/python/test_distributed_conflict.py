"""Tests for DistributedConflictDetector and MergeVerifier.

Covers the in-process advisory locking (no NATS needed), remote-intent
tracking, stale-lock reclamation, and MergeVerifier command execution +
auto-detection. NATS paths are exercised with stub publishers.
"""

from __future__ import annotations

import time

from ultimate_coders.agent.conflict import (
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
)
from ultimate_coders.agent.distributed_conflict import (
    DistributedConflictDetector,
    MergeVerifier,
)


def _intent(worker: str, path: str, start: int = 1, end: int = 10) -> EditIntent:
    return EditIntent(
        worker_id=worker,
        file_path=path,
        edit_type=EditType.MODIFY,
        regions=[LineRange(start=start, end=end)],
    )


class TestDistributedLocking:
    async def test_declare_acquires_lock_and_returns_no_conflict(self):
        d = DistributedConflictDetector(worker_id="w1")
        result, info = await d.declare_intent(_intent("w1", "a.rs"))
        assert result is ConflictResult.NO_CONFLICT
        assert info is None
        assert d.get_locked_files() == {"a.rs": "w1"}

    async def test_same_process_other_worker_same_file_is_conflict(self):
        d = DistributedConflictDetector(worker_id="w1")
        await d.declare_intent(_intent("w1", "a.rs"))
        # w2 in the same process tries the same file → in-process lock blocks it
        result, info = await d.declare_intent(_intent("w2", "a.rs"))
        assert result is ConflictResult.CONFLICTING
        assert info is not None
        assert "w1" in info.conflicting_workers

    async def test_release_then_reacquire_succeeds(self):
        # The lock owner is the detector's worker_id; releasing frees it for
        # a fresh claim by the same detector.
        d = DistributedConflictDetector(worker_id="w1")
        await d.declare_intent(_intent("w1", "a.rs"))
        await d.release_intent("a.rs")
        assert d.get_locked_files() == {}
        result, _ = await d.declare_intent(_intent("w1", "a.rs", start=20, end=30))
        assert result is ConflictResult.NO_CONFLICT
        assert d.get_locked_files() == {"a.rs": "w1"}

    async def test_stale_lock_from_other_owner_is_claimed(self):
        # A stale lock held by a DIFFERENT owner (injected directly) must be
        # reclaimed when this detector declares on the same file. Use a
        # disjoint region so the local ConflictDetector does not flag a
        # region overlap — we are testing the in-process lock staleness, not
        # the region-based local detector.
        d = DistributedConflictDetector(worker_id="w1", lock_timeout_seconds=0.0)
        d._file_locks["a.rs"] = ("w-other", time.time() - 100)
        time.sleep(0.01)
        result, _ = await d.declare_intent(_intent("w1", "a.rs", start=1, end=10))
        assert result is ConflictResult.NO_CONFLICT
        # Lock is now owned by this detector's worker_id.
        assert d.get_locked_files() == {"a.rs": "w1"}

    async def test_cleanup_stale_locks_removes_only_stale(self):
        d = DistributedConflictDetector(worker_id="w1", lock_timeout_seconds=0.0)
        await d.declare_intent(_intent("w1", "a.rs"))
        # Manually inject a stale lock owned by another worker.
        d._file_locks["b.rs"] = ("w2", time.time() - 100)
        removed = d.cleanup_stale_locks()
        assert removed == 1
        assert "b.rs" not in d.get_locked_files()
        # w1's own lock is never cleaned up as stale.
        assert "a.rs" in d.get_locked_files()

    async def test_different_files_no_conflict(self):
        d = DistributedConflictDetector(worker_id="w1")
        r1, _ = await d.declare_intent(_intent("w1", "a.rs"))
        r2, _ = await d.declare_intent(_intent("w2", "b.rs"))
        assert r1 is ConflictResult.NO_CONFLICT
        assert r2 is ConflictResult.NO_CONFLICT
        assert set(d.get_locked_files()) == {"a.rs", "b.rs"}


class TestRemoteIntents:
    async def test_receive_remote_intent_then_local_detects_overlap(self):
        d = DistributedConflictDetector(worker_id="w1")
        d.receive_remote_intent({
            "worker_id": "wX", "file_path": "a.rs",
            "edit_type": "modify", "timestamp": 1.0,
        })
        assert "a.rs" in d._remote_intents
        # Local declare for the same file+region now conflicts with the remote one.
        result, info = await d.declare_intent(_intent("w1", "a.rs", start=1, end=10))
        assert result is ConflictResult.CONFLICTING

    async def test_receive_remote_release_removes_intent(self):
        d = DistributedConflictDetector(worker_id="w1")
        d.receive_remote_intent({
            "worker_id": "wX", "file_path": "a.rs", "edit_type": "modify",
        })
        d.receive_remote_release({"worker_id": "wX", "file_path": "a.rs"})
        assert d._remote_intents["a.rs"] == []

    async def test_receive_remote_intent_ignores_missing_fields(self):
        d = DistributedConflictDetector(worker_id="w1")
        d.receive_remote_intent({"file_path": "", "worker_id": ""})
        assert d._remote_intents == {}


class TestNATSPathsAreOptional:
    async def test_nats_publish_failure_is_swallowed(self):
        class _BoomPublisher:
            async def publish_event(self, *a, **kw):
                raise RuntimeError("nats down")

        d = DistributedConflictDetector(
            worker_id="w1", nats_publisher=_BoomPublisher(),
        )
        # Must not raise despite the publisher throwing.
        result, _ = await d.declare_intent(_intent("w1", "a.rs"))
        assert result is ConflictResult.NO_CONFLICT

    async def test_nats_release_failure_is_swallowed(self):
        class _BoomPublisher:
            async def publish_event(self, *a, **kw):
                raise RuntimeError("nats down")

        d = DistributedConflictDetector(
            worker_id="w1", nats_publisher=_BoomPublisher(),
        )
        await d.declare_intent(_intent("w1", "a.rs"))
        await d.release_intent("a.rs")  # must not raise
        assert d.get_locked_files() == {}


class TestMergeVerifier:
    async def test_verify_success_command(self):
        v = MergeVerifier()
        result = await v.verify("x", "a.rs", verify_commands=["true"])
        assert result["passed"] is True
        assert result["failures"] == []

    async def test_verify_failure_command(self):
        v = MergeVerifier()
        result = await v.verify("x", "a.rs", verify_commands=["false"])
        assert result["passed"] is False
        assert result["failures"] == ["false"]

    async def test_verify_stops_at_first_failure(self):
        v = MergeVerifier()
        result = await v.verify("x", "a.rs", verify_commands=["false", "true"])
        assert result["passed"] is False
        # Only the failing command recorded; second never ran.
        assert result["failures"] == ["false"]

    async def test_no_commands_returns_passed(self):
        v = MergeVerifier()
        result = await v.verify("x", "a.txt", verify_commands=[])
        assert result["passed"] is True

    async def test_auto_detect_rust(self):
        v = MergeVerifier()
        assert v._auto_detect_commands("src/main.rs") == ["cargo check"]

    async def test_auto_detect_python(self):
        v = MergeVerifier()
        assert v._auto_detect_commands("foo.py") == ["python -m py_compile foo.py"]

    async def test_auto_detect_unknown_extension(self):
        v = MergeVerifier()
        assert v._auto_detect_commands("README.md") == []
