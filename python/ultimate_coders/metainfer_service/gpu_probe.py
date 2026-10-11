"""Trusted image-owned GPU kernel and actual native coding challenge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import torch
import triton
import triton.language as tl

from ultimate_coders.metainfer_service.coding import require_containment
from ultimate_coders.metainfer_service.ollama import OllamaProfile, native_client_identity


@triton.jit
def add(X, Y, Z, N: tl.constexpr, B: tl.constexpr):  # noqa: N803 -- Triton parameters
    offsets = tl.program_id(0) * B + tl.arange(0, B)
    value = tl.load(X + offsets, offsets < N, other=0) + tl.load(Y + offsets, offsets < N, other=0)
    tl.store(Z + offsets, value, offsets < N)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_containment()

    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise ValueError("This attestation profile requires one visible CUDA device")

    cases = []
    for n in (33, 4096, 65536):
        x = torch.arange(n, device="cuda", dtype=torch.float32) / 256
        y, z = torch.ones_like(x), torch.empty_like(x)
        add[(triton.cdiv(n, 256),)](x, y, z, n, 256)
        torch.cuda.synchronize()
        error = (z - (x + y)).abs().max().item()
        if error != 0:
            raise ValueError("GPU runtime kernel disagrees with the independent Torch result")
        cases.append({"size": n, "max_error": error})
    inventory = (
        subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=uuid,name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    if len(inventory) != 1:
        raise ValueError("This profile requires one physical GPU and no hidden device selection")
    identity, model, driver, memory = [part.strip() for part in inventory[0].split(",")]
    if model != torch.cuda.get_device_properties(0).name:
        raise ValueError("CUDA and device inventory disagree")
    profile = OllamaProfile.from_environment(dict(os.environ))
    nonce = os.urandom(16).hex()
    prompt = f"Return only this exact text, with no quotes or explanation: {nonce}"
    response = profile.chat([{"role": "user", "content": prompt}], tools=[], timeout=150)
    if response["message"]["content"].strip() != nonce or response["message"]["tool_calls"]:
        raise ValueError("Actual model inference did not answer the nonce challenge")
    hashes = native_client_identity()
    report = {
        "devices": [{"uuid": identity, "model": model, "driver": driver, "memory_mib": memory}],
        "runtime": {
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "triton": triton.__version__,
            "image_code": hashes,
        },
        "model_identity": {
            **profile.identity(),
            "client": "uc-native-ollama/1",
            "client_sha256": hashes["coding.py"],
        },
        "gpu_cases": cases,
        "model_probe": {
            "nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
            "response_sha256": hashlib.sha256(response["message"]["content"].encode()).hexdigest(),
        },
    }
    args.output.write_text(json.dumps(report))


if __name__ == "__main__":
    main()
