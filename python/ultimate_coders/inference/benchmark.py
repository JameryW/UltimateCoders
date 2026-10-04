"""Fixed argv benchmark harness protocol, shared across engineering domains."""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path

from .hardware import capture_environment
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
        except asyncio.TimeoutError:
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
    warmup_runs: int = 1
    repetitions: int = 3
    environment: dict[str, str] = field(default_factory=dict)

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
        for name, value, lower in (
            ("warmup_runs", self.warmup_runs, 0),
            ("repetitions", self.repetitions, 3),
        ):
            if type(value) is not int or not lower <= value <= 100:
                raise ValueError(f"{name} must be an integer between {lower} and 100")


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

    def checkpoint(self) -> dict[str, str]:
        return {str(path): digest for path, digest in (self._fingerprints or {}).items()}

    def restore_checkpoint(self, root: str, data: dict[str, str]) -> None:
        expected = {str((Path(root) / path).resolve()) for path in self.spec.protected_paths}
        actual = {str(Path(path).resolve()) for path in data}
        if actual != expected:
            raise ValueError("Checkpoint harness identity differs from configured protected paths")
        self._fingerprints = {Path(path): digest for path, digest in data.items()}
        self.verify_harness()

    async def measure(self, root: str, *, baseline: bool = False) -> BenchmarkResult:
        self.verify_harness()
        environment = await asyncio.to_thread(capture_environment, self.spec.environment)
        if self.spec.compile_command:
            await run_command(self.spec.compile_command, root, self.spec.timeout_seconds)
        command = (
            self.spec.baseline_command
            if baseline and self.spec.baseline_command
            else self.spec.command
        )
        samples = []
        for index in range(self.spec.warmup_runs + self.spec.repetitions):
            stdout = await run_command(command, root, self.spec.timeout_seconds)
            try:
                result = BenchmarkResult.from_dict(json.loads(stdout.strip().splitlines()[-1]))
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise ValueError("Benchmark must emit a final JSON BenchmarkResult") from exc
            if result.workload_id != self.spec.workload_id:
                raise ValueError("Benchmark workload_id differs from the configured workload")
            self.verify_harness()
            if not result.compile_success or not result.correctness:
                return result
            if index >= self.spec.warmup_runs:
                samples.append(result)
        if any(sample.metrics.keys() != samples[0].metrics.keys() for sample in samples):
            raise ValueError("Benchmark samples report different metrics")
        values = {key: [sample.metrics[key] for sample in samples] for key in samples[0].metrics}
        medians = {key: statistics.median(series) for key, series in values.items()}
        if "peak_memory_gb" in values:
            medians["peak_memory_gb"] = max(values["peak_memory_gb"])
        dispersion = {
            key: (
                100 * statistics.stdev(series) / statistics.mean(series)
                if statistics.mean(series)
                else 0
            )
            for key, series in values.items()
        }
        after = await asyncio.to_thread(capture_environment, self.spec.environment)
        if after["identity"] != environment["identity"]:
            raise ValueError("Benchmark hardware/runtime identity changed during measurement")
        identity = {
            **environment["identity"],
            "harness": sorted((path.name, digest) for path, digest in self._fingerprints.items()),
        }
        result = replace(
            samples[0],
            metrics=medians,
            numerical_error=(
                max(sample.numerical_error for sample in samples)
                if all(sample.numerical_error is not None for sample in samples)
                else None
            ),
            statistics={
                "count": len(samples),
                "warmup_runs": self.spec.warmup_runs,
                "samples": values,
                "dispersion_pct": dispersion,
                "environment": environment,
                "conditions_after": after["conditions"],
            },
            environment_id=hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode()
            ).hexdigest(),
        )
        self.verify_harness()
        if self.spec.profile_command:
            profile = await run_command(self.spec.profile_command, root, self.spec.timeout_seconds)
            self.verify_harness()
            result = replace(result, profile=profile)
        return result
