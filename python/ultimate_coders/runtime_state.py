"""Small transactional runtime records: PostgreSQL in clusters, SQLite locally.

Connections are reused in bounded pools. Mutations lock one record; callers
must run them off the event loop. A configured database never silently falls
back to process-local state.
"""

from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_pools: dict[tuple[int, str], Any] = {}
_pool_lock = threading.Lock()


@dataclass(frozen=True)
class VersionedRecord:
    data: dict
    version: int


class RecordConflictError(RuntimeError):
    """A concurrent operator or executor changed the record."""


class RuntimeState:
    def __init__(self, path: str | Path | None = None, *, database_url: str | None = None):
        self.url = (
            database_url if database_url is not None else os.environ.get("UC_DATABASE_URL", "")
        )
        self.path = Path(
            path or Path(os.environ.get("UC_RUNTIME_STATE_DIR", ".uc/runtime")) / "state.sqlite3"
        ).resolve()
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite = threading.local()
        with self._connect() as conn:
            if self.url:
                conn.execute("SET LOCAL lock_timeout = '30s'")
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x55434D4C,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS uc_runtime_records ("
                "namespace TEXT NOT NULL, record_key TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(namespace, record_key))"
            )
            columns = {
                "record_state": "TEXT NOT NULL DEFAULT ''",
                "task_id": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "next_retry_at": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "version": "BIGINT NOT NULL DEFAULT 0",
                "workspace": "TEXT NOT NULL DEFAULT ''",
                "created_at": "DOUBLE PRECISION NOT NULL DEFAULT 0",
                "delivered_at": "DOUBLE PRECISION NOT NULL DEFAULT 0",
            }
            if not self.url:
                conn.execute("BEGIN IMMEDIATE")
                existing = {row[1] for row in conn.execute("PRAGMA table_info(uc_runtime_records)")}
            else:
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='uc_runtime_records' AND table_schema=current_schema()"
                    )
                }
            migrated = False
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE uc_runtime_records ADD COLUMN {name} {definition}")
                    migrated = True
            if migrated:
                # One-time migration: old pending records must be visible to indexed replay.
                for namespace, key, raw in conn.execute(
                    "SELECT namespace, record_key, data FROM uc_runtime_records"
                ).fetchall():
                    data = json.loads(raw)
                    p = "%s" if self.url else "?"
                    conn.execute(
                        f"UPDATE uc_runtime_records SET record_state={p}, "
                        f"task_id={p}, workspace={p}, "
                        f"created_at={p}, delivered_at={p} "
                        f"WHERE namespace={p} AND record_key={p}",
                        (
                            *self._metadata(namespace, data),
                            time.time(),
                            data.get("delivered_at", 0),
                            namespace,
                            key,
                        ),
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS uc_runtime_pending "
                "ON uc_runtime_records(namespace, record_state, next_retry_at, record_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS uc_runtime_task "
                "ON uc_runtime_records(namespace, task_id, record_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS uc_runtime_workspace "
                "ON uc_runtime_records(namespace, workspace, record_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS uc_runtime_retention "
                "ON uc_runtime_records(namespace, record_state, delivered_at, record_key)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS uc_runtime_audit ("
                "audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
                "record_key TEXT NOT NULL, version BIGINT NOT NULL, "
                "created_at DOUBLE PRECISION NOT NULL, data TEXT NOT NULL)"
            )

    def _connect(self):
        if self.url:
            from psycopg_pool import ConnectionPool

            key = (os.getpid(), self.url)
            with _pool_lock:
                if key not in _pools:
                    pool = ConnectionPool(
                        self.url,
                        min_size=0,
                        max_size=8,
                        timeout=15,
                        kwargs={"connect_timeout": 10},
                        open=True,
                    )
                    _pools[key] = pool
                    atexit.register(pool.close)
            return _pools[key].connection()
        return self._sqlite_connection()

    @contextmanager
    def _sqlite_connection(self):
        if getattr(self._sqlite, "pid", None) != os.getpid():
            self._sqlite.conn = sqlite3.connect(self.path, timeout=30)
            self._sqlite.pid = os.getpid()
        with self._sqlite.conn as conn:
            yield conn

    @staticmethod
    def _metadata(namespace: str, data: dict) -> tuple[str, str, str]:
        status = data.get("state", data.get("status", ""))
        if namespace == "result_outbox":
            status = (
                "archived"
                if data.get("tombstone")
                else ("delivered" if data.get("delivered") else "pending")
            )
        identity = data.get("identity") or {}
        return (
            str(status),
            str(identity.get("graph_id", data.get("task_id", ""))),
            str(data.get("workspace", data.get("handle", {}).get("worktree_path", ""))),
        )

    def mutate(
        self,
        namespace: str,
        key: str,
        change: Callable[[dict], dict],
        *,
        expected_version: int | None = None,
        audit: dict | None = None,
    ) -> dict:
        with self._connect() as conn:
            placeholder = "%s" if self.url else "?"
            params = (namespace, key)
            if not self.url:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO uc_runtime_records(namespace, record_key, data) "
                f"VALUES ({placeholder}, {placeholder}, '{{}}') "
                "ON CONFLICT(namespace, record_key) DO NOTHING",
                params,
            )
            query = (
                f"SELECT data, version FROM uc_runtime_records WHERE namespace={placeholder} "
                f"AND record_key={placeholder}" + (" FOR UPDATE" if self.url else "")
            )
            raw, version = conn.execute(query, params).fetchone()
            if expected_version is not None and version != expected_version:
                raise RecordConflictError("Record version changed; reload before reconciliation")
            current = json.loads(raw)
            updated = change(current)
            if not isinstance(updated, dict):
                raise TypeError("Runtime records must be objects")
            conn.execute(
                f"UPDATE uc_runtime_records SET data={placeholder}, record_state={placeholder}, "
                f"task_id={placeholder}, workspace={placeholder}, updated_at={placeholder}, "
                f"next_retry_at={placeholder}, "
                f"created_at=CASE WHEN created_at=0 THEN {placeholder} ELSE created_at END, "
                f"delivered_at={placeholder}, "
                f"version=version+1 WHERE namespace={placeholder} "
                f"AND record_key={placeholder}",
                (
                    json.dumps(updated),
                    *self._metadata(namespace, updated),
                    time.time(),
                    updated.get("next_retry_at", 0),
                    time.time(),
                    updated.get("delivered_at", 0),
                    *params,
                ),
            )
            if audit is not None:
                import uuid

                conn.execute(
                    "INSERT INTO uc_runtime_audit VALUES (" + ",".join([placeholder] * 6) + ")",
                    (uuid.uuid4().hex, namespace, key, version + 1, time.time(), json.dumps(audit)),
                )
            return updated

    def versioned_get(self, namespace: str, key: str) -> VersionedRecord | None:
        with self._connect() as conn:
            p = "%s" if self.url else "?"
            row = conn.execute(
                f"SELECT data, version FROM uc_runtime_records "
                f"WHERE namespace={p} AND record_key={p}",
                (namespace, key),
            ).fetchone()
            return VersionedRecord(json.loads(row[0]), row[1]) if row else None

    def query(
        self,
        namespace: str,
        *,
        status: str | None = None,
        task_id: str | None = None,
        after: str = "",
        limit: int = 100,
        due: bool = False,
        workspace: str | None = None,
        prefix: str | None = None,
        delivered_before: float | None = None,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Query limit must be between 1 and 1000")
        p = "%s" if self.url else "?"
        where, args = [f"namespace={p}", f"record_key>{p}"], [namespace, after]
        for column, value in (
            ("record_state", status),
            ("task_id", task_id),
            ("workspace", workspace),
        ):
            if value is not None:
                where.append(f"{column}={p}")
                args.append(value)
        if due:
            where.append(f"next_retry_at<={p}")
            args.append(time.time())
        if prefix is not None:
            where.append(f"record_key LIKE {p} ESCAPE '!'")
            args.append(prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
        if delivered_before is not None:
            where.append(f"delivered_at<={p}")
            args.append(delivered_before)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_key, data FROM uc_runtime_records WHERE "
                + " AND ".join(where)
                + f" ORDER BY record_key LIMIT {p}",
                (*args, limit),
            ).fetchall()
            return [{"key": key, **json.loads(raw)} for key, raw in rows]

    def archive_delivered(self, *, older_than: float, limit: int = 100) -> int:
        """Keep a compact durable tombstone so old dispatches never execute again."""
        count = 0
        for record in self.query(
            "result_outbox", status="delivered", limit=limit, delivered_before=older_than
        ):
            if record.get("delivered_at", 0) > older_than or record.get("tombstone"):
                continue

            def compact(old):
                if old.get("delivered") and old.get("delivered_at", 0) <= older_than:
                    return {
                        "delivered": True,
                        "tombstone": True,
                        "delivered_at": old.get("delivered_at", 0),
                    }
                return old

            self.mutate("result_outbox", record["key"], compact)
            count += 1
        return count

    def get(self, namespace: str, key: str) -> dict | None:
        with self._connect() as conn:
            placeholder = "%s" if self.url else "?"
            row = conn.execute(
                f"SELECT data FROM uc_runtime_records WHERE namespace={placeholder} "
                f"AND record_key={placeholder}",
                (namespace, key),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def records(self, namespace: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            placeholder = "%s" if self.url else "?"
            rows = conn.execute(
                f"SELECT record_key, data FROM uc_runtime_records WHERE namespace={placeholder}",
                (namespace,),
            ).fetchall()
            return [{"key": row[0], **json.loads(row[1])} for row in rows]

    def statistics(self, namespace: str) -> dict:
        with self._connect() as conn:
            p = "%s" if self.url else "?"
            rows = conn.execute(
                f"SELECT record_state, COUNT(*), MIN(created_at) FROM uc_runtime_records "
                f"WHERE namespace={p} GROUP BY record_state",
                (namespace,),
            ).fetchall()
            return {
                status: {"count": count, "oldest_age_seconds": max(0, time.time() - updated)}
                for status, count, updated in rows
            }


class _SQLiteConnection:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=30)

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        try:
            self.conn.__exit__(*exc)
        finally:
            self.conn.close()


def atomic_json(path: Path, data: dict) -> None:
    """Publish a complete checkpoint without exposing a partially written file."""
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def atomic_write(path: Path, data: bytes) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """Read liveness without sending a signal (os.kill(pid, 0) kills on Windows)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not evidence of death
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            api.CloseHandle(wintypes.HANDLE(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_identity(pid: int) -> str | None:
    """Birth identity avoids treating a reused PID as the previous owner."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(wintypes.HANDLE(handle), *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            api.CloseHandle(wintypes.HANDLE(handle))
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + stat[19]
    except (OSError, IndexError):
        return None


def process_matches(pid: int, identity: str | None = None) -> bool:
    if not pid_alive(pid):
        return False
    actual = process_identity(pid)
    return identity is None or actual is None or actual == identity
