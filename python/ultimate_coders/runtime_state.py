"""Small transactional runtime records: PostgreSQL in clusters, SQLite locally.

Each call owns and closes its connection. Mutations lock one record; callers
must run them off the event loop. A configured database never silently falls
back to process-local state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable


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
        with self._connect() as conn:
            if self.url:
                conn.execute("SET LOCAL lock_timeout = '30s'")
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x55434D4C,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS uc_runtime_records ("
                "namespace TEXT NOT NULL, record_key TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(namespace, record_key))"
            )

    def _connect(self):
        if self.url:
            import psycopg

            return psycopg.connect(self.url, connect_timeout=10)
        return _SQLiteConnection(self.path)

    def mutate(self, namespace: str, key: str, change: Callable[[dict], dict]) -> dict:
        with self._connect() as conn:
            placeholder = "%s" if self.url else "?"
            params = (namespace, key)
            if not self.url:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"INSERT INTO uc_runtime_records VALUES ({placeholder}, {placeholder}, '{{}}') "
                "ON CONFLICT(namespace, record_key) DO NOTHING",
                params,
            )
            query = (
                f"SELECT data FROM uc_runtime_records WHERE namespace={placeholder} "
                f"AND record_key={placeholder}" + (" FOR UPDATE" if self.url else "")
            )
            current = json.loads(conn.execute(query, params).fetchone()[0])
            updated = change(current)
            conn.execute(
                f"UPDATE uc_runtime_records SET data={placeholder} WHERE namespace={placeholder} "
                f"AND record_key={placeholder}",
                (json.dumps(updated), *params),
            )
            return updated

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
