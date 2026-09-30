"""Contained argv processes: POSIX sessions and Windows kill-on-close jobs."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


class ProcessTree:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.pid: int | None = None
        self.job = None
        variables = env if env is not None else os.environ
        directory = variables.get("UC_INFERENCE_PROCESS_REGISTRY")
        self.registry = Path(directory) if directory else None
        self.owner_pid = int(variables.get("UC_INFERENCE_OWNER_PID", os.getpid()))
        self.record: Path | None = None
        if os.name != "nt":
            return
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", w.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("limits", Limits),
                ("io", Counters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, arguments, result in (
            ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.job = self.api.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid: int) -> None:
        self.pid = pid
        if self.job is None:
            if self.registry is not None:
                self.record = self.registry / f"group-{pid}.json"
                self.record.write_text(json.dumps({"owner": self.owner_pid, "group": pid}))
            return
        handle = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(handle)

    def close(self) -> None:
        if self.job is not None:
            self.api.CloseHandle(self.job)
            self.job = None
        elif os.name == "posix" and self.pid is not None:
            try:
                # Freeze the owner before enumerating child sessions, so it
                # cannot launch another command behind the cleanup scan.
                os.killpg(self.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
            # Nested commands use their own sessions so their local deadlines
            # cannot kill the runner. The outer runner's owner registry covers
            # those sessions when forced termination bypasses runner cleanup.
            if self.registry is not None:
                for record in self.registry.glob("group-*.json"):
                    try:
                        entry = json.loads(record.read_text())
                        if entry["owner"] == self.pid and type(entry["group"]) is int:
                            os.killpg(entry["group"], signal.SIGKILL)
                    except (FileNotFoundError, ProcessLookupError, ValueError, KeyError):
                        pass
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.record is not None:
            self.record.unlink(missing_ok=True)
        self.pid = None


async def spawn_command(command: list[str], **kwargs: Any) -> tuple[Any, ProcessTree]:
    tree = ProcessTree(kwargs.get("env"))
    proc = None
    try:
        # Gate execution until containment/registration is complete on both
        # platforms. If the owner dies before release, EOF ends the wrapper
        # without starting the real command.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            json.dumps(command),
            stdin=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
            **kwargs,
        )
        tree.attach(proc.pid)
        proc.stdin.write(b"1")
        await proc.stdin.drain()
        proc.stdin.close()
        return proc, tree
    except BaseException:
        tree.close()
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                pass
        raise


if __name__ == "__main__":
    if sys.argv[1] != "--child" or sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(1)
    if os.environ.get("UC_INFERENCE_PROCESS_REGISTRY"):
        os.environ.setdefault("UC_INFERENCE_OWNER_PID", str(os.getpid()))
    raise SystemExit(subprocess.call(json.loads(sys.argv[2]), stdin=subprocess.DEVNULL))
