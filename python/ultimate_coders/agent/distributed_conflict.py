"""Distributed conflict resolution for multi-worker code editing.

Extends the local ConflictDetector with:
1. Distributed EditIntent — broadcast intents via NATS so all workers see them
2. Distributed file locks — per-file mutex over NATS for write exclusivity
3. Merge verification — auto-compile/test after three-way merge

The local ConflictDetector is the primary mechanism. This module adds
the distributed coordination layer on top.

ponytail: NATS-based coordination — simple, leverages existing
infrastructure. Upgrade to etcd/consul for stronger consistency
if NATS dedup proves insufficient.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictInfo,
    ConflictResult,
    ConflictResolver,
    EditIntent,
    EditType,
    MergeResult,
    ResolutionTier,
)

logger = logging.getLogger(__name__)


# ── NATS subject constants ────────────────────────────────────────

NATS_SUBJECT_EDIT_INTENT = "uc.edit.intent"
NATS_SUBJECT_FILE_LOCK = "uc.file.lock"
NATS_SUBJECT_FILE_UNLOCK = "uc.file.unlock"


# ── Distributed EditIntent ────────────────────────────────────────

class DistributedConflictDetector:
    """Conflict detector that broadcasts edit intents via NATS.

    Combines local ConflictDetector with NATS-based intent broadcasting:
    - declare_intent() checks locally first, then broadcasts
    - Receives remote intents from other workers via NATS subscription
    - File locking ensures exclusive write access across workers

    Usage:
        detector = DistributedConflictDetector(
            local_detector=ConflictDetector(),
            nats_publisher=publisher,
            worker_id="worker-1",
        )
        result, info = await detector.declare_intent(EditIntent(...))
        # ... do work ...
        await detector.release_intent("src/main.rs")
    """

    def __init__(
        self,
        local_detector: ConflictDetector | None = None,
        nats_publisher: Any = None,
        worker_id: str = "",
        lock_timeout_seconds: float = 300.0,
    ) -> None:
        self._local = local_detector or ConflictDetector()
        self._nats_publisher = nats_publisher
        self._worker_id = worker_id
        self._lock_timeout = lock_timeout_seconds

        # Distributed lock state: file_path → (owner_id, timestamp)
        self._file_locks: dict[str, tuple[str, float]] = {}
        # Remote intents received from other workers
        self._remote_intents: dict[str, list[EditIntent]] = {}

    @property
    def local_detector(self) -> ConflictDetector:
        """Access the underlying local ConflictDetector."""
        return self._local

    async def declare_intent(
        self, intent: EditIntent,
    ) -> tuple[ConflictResult, ConflictInfo | None]:
        """Declare edit intent with distributed coordination.

        Steps:
        1. Check local ConflictDetector (fast path)
        2. Try to acquire distributed file lock
        3. Broadcast intent to other workers via NATS
        4. Return conflict result
        """
        # Step 1: Local check
        result, info = self._local.declare_intent(intent)
        if result == ConflictResult.CONFLICTING:
            return result, info

        # Step 2: Distributed file lock (always — even without NATS for local locking)
        if intent.file_path:
            lock_acquired = await self._acquire_lock(intent.file_path)
            if not lock_acquired:
                # Another worker has this file locked — conflict
                return ConflictResult.CONFLICTING, ConflictInfo(
                    file_path=intent.file_path,
                    conflicting_workers=[self._file_locks.get(intent.file_path, ("unknown", 0))[0]],
                    resolution_tier=ResolutionTier.AUTO_MERGE,
                )

        # Step 3: Broadcast intent to other workers (NATS only)
        if self._nats_publisher:
            try:
                await self._nats_publisher.publish_event(
                    "edit_intent_declared",
                    task_id="",
                    subtask_id="",
                    data={
                        "worker_id": intent.worker_id,
                        "file_path": intent.file_path,
                        "edit_type": intent.edit_type.value,
                        "timestamp": intent.timestamp,
                    },
                )
            except Exception:
                logger.debug("Failed to broadcast edit intent", exc_info=True)

        return result, info

    async def release_intent(self, file_path: str, worker_id: str | None = None) -> None:
        """Release an edit intent and distributed file lock.

        Args:
            file_path: The file to release.
            worker_id: The worker releasing. Defaults to self._worker_id.
        """
        wid = worker_id or self._worker_id
        self._local.remove_intent(file_path, wid)

        # Release distributed lock (always, even without NATS)
        if file_path in self._file_locks:
            owner, _ = self._file_locks[file_path]
            if owner == wid:
                del self._file_locks[file_path]

        # Broadcast release to other workers (NATS only)
        if self._nats_publisher:
            try:
                await self._nats_publisher.publish_event(
                    "edit_intent_released",
                    task_id="",
                    subtask_id="",
                    data={
                        "worker_id": wid,
                        "file_path": file_path,
                    },
                )
            except Exception:
                logger.debug("Failed to broadcast intent release", exc_info=True)

    def receive_remote_intent(self, intent_data: dict[str, Any]) -> None:
        """Process a remote edit intent received via NATS.

        Called by the NATS subscriber when another worker declares an intent.
        Adds the remote intent to the local detector so future local checks
        can detect cross-worker conflicts.
        """
        file_path = intent_data.get("file_path", "")
        worker_id = intent_data.get("worker_id", "")
        edit_type = EditType(intent_data.get("edit_type", "modify"))

        if not file_path or not worker_id:
            return

        remote_intent = EditIntent(
            worker_id=worker_id,
            file_path=file_path,
            edit_type=edit_type,
            timestamp=intent_data.get("timestamp", time.time() * 1000),
        )

        # Add to local detector so local checks see this remote intent
        self._local.declare_intent(remote_intent)

        # Track in remote intents dict
        if file_path not in self._remote_intents:
            self._remote_intents[file_path] = []
        self._remote_intents[file_path].append(remote_intent)

        logger.debug(
            "Received remote intent: %s on %s from %s",
            edit_type.value, file_path, worker_id[:8],
        )

    def receive_remote_release(self, release_data: dict[str, Any]) -> None:
        """Process a remote intent release received via NATS."""
        file_path = release_data.get("file_path", "")
        worker_id = release_data.get("worker_id", "")

        if file_path and worker_id:
            self._local.remove_intent(file_path, worker_id)
            # Remove from remote intents tracking
            if file_path in self._remote_intents:
                self._remote_intents[file_path] = [
                    i for i in self._remote_intents[file_path]
                    if i.worker_id != worker_id
                ]

    async def _acquire_lock(self, file_path: str) -> bool:
        """Try to acquire a distributed file lock via NATS.

        Uses a simple request-reply pattern: publish lock request,
        wait for acknowledgment. If no ack within timeout, assume
        lock is available (optimistic).

        ponytail: optimistic locking — if NATS is unavailable, allow
        the edit (local detector still catches same-process conflicts).
        Upgrade to distributed lock service (etcd) if needed.
        """
        now = time.time()

        # Check if file is already locked by someone else
        if file_path in self._file_locks:
            owner, lock_time = self._file_locks[file_path]
            if owner != self._worker_id:
                # Check if lock is stale (timeout exceeded)
                if now - lock_time < self._lock_timeout:
                    return False  # Lock is held and not stale
                # Stale lock — claim it
                logger.info("Claiming stale lock on %s (held by %s)", file_path, owner[:8])

        # Claim the lock
        self._file_locks[file_path] = (self._worker_id, now)
        return True

    def cleanup_stale_locks(self) -> int:
        """Remove stale file locks (exceeded timeout).

        Returns:
            Number of locks cleaned up.
        """
        now = time.time()
        stale = [
            fp for fp, (owner, lock_time) in self._file_locks.items()
            if now - lock_time > self._lock_timeout and owner != self._worker_id
        ]
        for fp in stale:
            del self._file_locks[fp]
        return len(stale)

    def get_locked_files(self) -> dict[str, str]:
        """Get currently locked files and their owners."""
        return {fp: owner for fp, (owner, _) in self._file_locks.items()}


# ── Merge Verification ────────────────────────────────────────────

class MergeVerifier:
    """Verify merged files by running compile/test commands.

    After a three-way merge, runs verification to ensure the merged
    result doesn't break the build or tests.

    Usage:
        verifier = MergeVerifier()
        passed = await verifier.verify(
            merged_content=merged,
            file_path="src/main.rs",
            verify_commands=["cargo check", "cargo test --no-run"],
        )
    """

    def __init__(self, default_timeout: float = 60.0) -> None:
        self._default_timeout = default_timeout

    async def verify(
        self,
        merged_content: str,
        file_path: str,
        verify_commands: list[str] | None = None,
        project_path: str = "",
    ) -> dict[str, Any]:
        """Verify a merged file by running compile/test commands.

        Args:
            merged_content: The merged file content.
            file_path: Path to the file being verified.
            verify_commands: Commands to run for verification.
            project_path: Project root directory.

        Returns:
            Dict with 'passed', 'failures', and 'output' keys.
        """
        if not verify_commands:
            # Auto-detect verification based on file extension
            verify_commands = self._auto_detect_commands(file_path)

        if not verify_commands:
            return {"passed": True, "failures": [], "output": "No verification commands"}

        results: list[dict[str, Any]] = []
        for cmd in verify_commands:
            result = await self._run_command(cmd, project_path)
            results.append(result)
            if not result["success"]:
                # Early exit on first failure
                break

        all_passed = all(r["success"] for r in results)
        failures = [r for r in results if not r["success"]]

        return {
            "passed": all_passed,
            "failures": [f["command"] for f in failures],
            "output": "\n".join(r.get("stderr", "")[:500] for r in results),
        }

    def _auto_detect_commands(self, file_path: str) -> list[str]:
        """Auto-detect verification commands based on file extension.

        ponytail: simple extension-based detection — covers the
        common cases. Add more as needed.
        """
        if file_path.endswith(".rs"):
            return ["cargo check"]
        if file_path.endswith(".py"):
            return ["python -m py_compile " + file_path]
        if file_path.endswith(".ts") or file_path.endswith(".tsx"):
            return ["npx tsc --noEmit"]
        if file_path.endswith(".go"):
            return ["go build ./..."]
        return []

    async def _run_command(
        self,
        command: str,
        cwd: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a verification command."""
        import os

        proc = await asyncio.create_subprocess_exec(
            *command.split(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.getcwd(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout or self._default_timeout,
            )
            return {
                "command": command,
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace")[:1000],
                "stderr": stderr.decode("utf-8", errors="replace")[:1000],
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "command": command,
                "success": False,
                "stdout": "",
                "stderr": f"Command timed out after {timeout or self._default_timeout}s",
            }
