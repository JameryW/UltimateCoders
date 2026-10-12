"""Runtime record serialization across actual concurrent database sessions."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture
def locked_windows_checkpoint(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows sharing violations require actual Windows file handles")
    import ctypes
    from ctypes import wintypes

    path = tmp_path / "checkpoint.json"
    path.write_bytes(b"original")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.restype = wintypes.HANDLE
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    closed = False

    def release():
        nonlocal closed
        if not closed:
            assert api.CloseHandle(handle)
            closed = True

    try:
        yield path, release
    finally:
        release()


def test_atomic_checkpoint_recovers_when_real_windows_reader_releases(
    locked_windows_checkpoint, monkeypatch
):
    from ultimate_coders.runtime_state import atomic_write

    path, release = locked_windows_checkpoint
    replace = os.replace
    attempts = []

    def observed_replace(source, target):
        attempts.append(target)
        try:
            return replace(source, target)
        except PermissionError as exc:
            assert exc.winerror in (5, 32)
            release()
            raise

    monkeypatch.setattr(os, "replace", observed_replace)
    atomic_write(path, b"complete replacement")
    assert len(attempts) == 2
    assert path.read_bytes() == b"complete replacement"
    assert list(path.parent.iterdir()) == [path]


def test_atomic_checkpoint_preserves_old_bytes_when_windows_reader_stays_locked(
    locked_windows_checkpoint, monkeypatch
):
    from ultimate_coders.runtime_state import atomic_write

    path, _ = locked_windows_checkpoint
    replace = os.replace
    attempts = []

    def observed_replace(source, target):
        attempts.append(target)
        return replace(source, target)

    monkeypatch.setattr(os, "replace", observed_replace)
    with pytest.raises(PermissionError):
        atomic_write(path, b"unpublished replacement")
    assert len(attempts) == 5
    assert path.read_bytes() == b"original"
    assert list(path.parent.iterdir()) == [path]


def exercise_records(factory):
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: factory(), range(8)))
        list(
            pool.map(
                lambda index: stores[index % 8].mutate(
                    "fixture", "counter", lambda old: {"count": old.get("count", 0) + 1}
                ),
                range(80),
            )
        )
    assert stores[0].get("fixture", "counter") == {"count": 80}
    assert stores[1].records("fixture") == [{"key": "counter", "count": 80}]


def test_sqlite_concurrent_records(tmp_path):
    exercise_records(lambda: RuntimeState(tmp_path / "state.db", database_url=""))


@pytest.mark.integration
def test_postgres_cold_start_and_concurrent_records():
    url = os.environ.get("UC_RUNTIME_TEST_PG_URL")
    if not url:
        pytest.skip("Set UC_RUNTIME_TEST_PG_URL to test an isolated PostgreSQL database")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    name = "uc_reliability_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        options = conninfo_to_dict(url)
        options["dbname"] = name
        from psycopg.conninfo import make_conninfo

        isolated = make_conninfo(**options)
        exercise_records(lambda: RuntimeState(database_url=isolated))
    finally:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
