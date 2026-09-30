"""Fixed argv benchmark harness protocol, shared across engineering domains."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .models import BenchmarkResult, nonnegative_number
from .process import spawn_command


async def run_command(
    command: list[str],
    cwd: str,
    timeout_seconds: float = 300,
    env: dict[str, str] | None = None,
) -> str:
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or not arg for arg in command)
    ):
        raise ValueError("Commands must be nonempty argv lists")
    proc, tree = await spawn_command(
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read(stream: asyncio.StreamReader) -> bytes:
        chunks, size = [], 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > 1024 * 1024:
                raise RuntimeError("Command output exceeds 1 MiB")
            chunks.append(chunk)

    readers = asyncio.gather(read(proc.stdout), read(proc.stderr), proc.wait())
    try:
        stdout, stderr, code = await asyncio.wait_for(
            readers,
            timeout_seconds,
        )
        if code:
            raise RuntimeError(
                f"Command failed with exit {code}: {stderr.decode(errors='replace')[-500:]}"
            )
        return stdout.decode("utf-8")
    except BaseException:
        tree.close()
        readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            pass
        raise
    finally:
        tree.close()


@dataclass(frozen=True)
class BenchmarkSpec:
    command: list[str]
    workload_id: str
    timeout_seconds: float = 300
    compile_command: list[str] | None = None
    profile_command: list[str] | None = None
    protected_paths: list[str] = field(default_factory=list)
    baseline_command: list[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workload_id, str) or not self.workload_id:
            raise ValueError("workload_id is required")
        for command in (
            self.command,
            self.compile_command,
            self.profile_command,
            self.baseline_command,
        ):
            if command is not None and (
                not isinstance(command, list)
                or not command
                or any(not isinstance(arg, str) or not arg for arg in command)
            ):
                raise ValueError("Benchmark commands must be nonempty argv lists")
        if nonnegative_number(self.timeout_seconds, "timeout_seconds") == 0:
            raise ValueError("timeout_seconds must be positive")


class BenchmarkRunner:
    def __init__(self, spec: BenchmarkSpec) -> None:
        self.spec = spec
        self._fingerprints: dict[Path, str] | None = None

    def protect(self, root: str) -> None:
        paths = [
            Path(p) if Path(p).is_absolute() else Path(root) / p for p in self.spec.protected_paths
        ]
        if not paths:
            raise ValueError("protected_paths must identify the immutable benchmark harness")
        self._fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    def verify_harness(self) -> None:
        for path, expected in (self._fingerprints or {}).items():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Benchmark harness changed: {path.name}")

    async def measure(self, root: str, *, baseline: bool = False) -> BenchmarkResult:
        self.verify_harness()
        if self.spec.compile_command:
            await run_command(self.spec.compile_command, root, self.spec.timeout_seconds)
        command = (
            self.spec.baseline_command
            if baseline and self.spec.baseline_command
            else self.spec.command
        )
        stdout = await run_command(command, root, self.spec.timeout_seconds)
        try:
            result = BenchmarkResult.from_dict(json.loads(stdout.strip().splitlines()[-1]))
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError("Benchmark must emit a final JSON BenchmarkResult") from exc
        if result.workload_id != self.spec.workload_id:
            raise ValueError("Benchmark workload_id differs from the configured workload")
        self.verify_harness()
        if self.spec.profile_command:
            profile = await run_command(self.spec.profile_command, root, self.spec.timeout_seconds)
            self.verify_harness()
            result = replace(result, profile=profile)
        return result
