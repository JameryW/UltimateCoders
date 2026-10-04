"""Actual runtime identity and GPU measurement conditions, without importing torch."""

from __future__ import annotations

import csv
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def capture_environment(declared: dict[str, str]) -> dict:
    devices, conditions = [], []
    tool = shutil.which("nvidia-smi")
    if tool:
        result = subprocess.run(
            [
                tool,
                "--query-gpu=index,uuid,name,driver_version,memory.total,temperature.gpu,clocks.current.sm,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("GPU identity query failed")
        visible = os.environ.get("CUDA_VISIBLE_DEVICES")
        selectors = visible.split(",") if visible is not None else None
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) != 8:
                raise ValueError("GPU identity query returned malformed evidence")
            index, identity, model, driver, memory, temperature, clock, utilization = [
                item.strip() for item in row
            ]
            if selectors is not None and not any(
                value == index or identity.startswith(value) for value in selectors if value
            ):
                continue
            devices.append(
                {"uuid": identity, "model": model, "driver": driver, "memory_mib": memory}
            )
            conditions.append(
                {
                    "uuid": identity,
                    "temperature_c": temperature,
                    "sm_clock_mhz": clock,
                    "utilization_pct": utilization,
                }
            )
    if declared.get("require_gpu", "false").lower() == "true" and not devices:
        raise ValueError("Declared GPU workload has no observable GPU")
    for field, actual_key in (
        ("gpu_uuid", "uuid"),
        ("gpu_model", "model"),
        ("gpu_driver", "driver"),
    ):
        if declared.get(field) and not any(item[actual_key] == declared[field] for item in devices):
            raise ValueError(f"Declared {field} differs from actual hardware")
    versions = {}
    for package in ("torch", "triton", "numpy", "sglang", "vllm", "nvidia-cuda-runtime-cu12"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    compiler = shutil.which("nvcc")
    compiler_version = (
        subprocess.run(
            [compiler, "--version"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        if compiler
        else None
    )
    weights = []
    if declared.get("model_path"):
        root = Path(declared["model_path"]).resolve(strict=True)
        files = (
            [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        )
        for path in files:
            stat = path.stat()
            weights.append(
                {
                    "file": path.name if root.is_file() else str(path.relative_to(root)),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        # The manifest identity records actual files; callers can additionally
        # provide a verified model checksum for reproducible release evidence.
    identity = {
        "declared": declared,
        "platform": platform.platform(),
        "python": sys.version,
        "executable": sys.executable,
        "devices": devices,
        "dependencies": versions,
        "cuda_compiler": compiler_version,
        "weights": weights,
    }
    return {"identity": identity, "conditions": conditions}


def condition_drift(baseline: dict, candidate: dict) -> list[str]:
    if (
        baseline.get("identity", {}).get("declared", {}).get("require_gpu", "false").lower()
        != "true"
    ):
        return []
    reasons = []
    for old in baseline.get("conditions", []):
        new = next(
            (item for item in candidate.get("conditions", []) if item["uuid"] == old["uuid"]), None
        )
        if new is None:
            continue
        try:
            if abs(float(new["temperature_c"]) - float(old["temperature_c"])) > 10:
                reasons.append("GPU temperature drift exceeds 10 C")
            clock = float(old["sm_clock_mhz"])
            if clock and abs(float(new["sm_clock_mhz"]) - clock) / clock > 0.05:
                reasons.append(
                    "GPU clock drift exceeds 5 percent; remeasure under stable conditions"
                )
            if float(new["utilization_pct"]) > 5 or float(old["utilization_pct"]) > 5:
                reasons.append("GPU is busy outside the timed workload")
        except (ValueError, KeyError):
            reasons.append("GPU measurement conditions are unavailable")
    return reasons
