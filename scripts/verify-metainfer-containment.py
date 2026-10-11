#!/usr/bin/env python3
"""Real detached-writer/cancel/recovery gate; does not claim GPU optimization acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from ultimate_coders.inference.service_contract import validate_quiescence
from ultimate_coders.metainfer_service.app import ServiceConfig
from ultimate_coders.metainfer_service.jobs import ContainedJobs, checkpoint
from ultimate_coders.metainfer_service.oci import LocalOCI
from ultimate_coders.metainfer_service.upstream import PINNED_REVISION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, required=True, help="New disposable evidence directory"
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", choices=("podman", "docker"), default="podman")
    parser.add_argument("--phase", choices=("launch", "recover"))
    args = parser.parse_args()
    root = args.root.resolve()
    if not args.phase:
        root.mkdir(parents=True, exist_ok=False)
        for phase in ("launch", "recover"):
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--root",
                    str(root),
                    "--image",
                    args.image,
                    "--runtime",
                    args.runtime,
                    "--phase",
                    phase,
                ],
                check=True,
                timeout=60,
            )
        print(json.dumps(json.loads((root / "result.json").read_text()), indent=2))
        return
    workspace = root / "worktrees" / "job"
    workspace.mkdir(parents=True, exist_ok=True)
    config = ServiceConfig(
        root / "control",
        (root / "worktrees",),
        "containment-probe-" + hashlib.sha256(str(root).encode()).hexdigest()[:16],
        PINNED_REVISION,
    )
    runtime = LocalOCI(args.runtime, args.image)
    jobs = ContainedJobs(config, runtime)
    target = workspace / "writer.txt"
    if args.phase == "launch":
        # New session deliberately escapes process-group signalling. The private
        # OCI PID namespace/cgroup must still contain and stop this writer.
        child = (
            "import time,os; f=open(" + repr(str(target)) + ", 'ab', buffering=0); "
            "\nwhile True: f.write(b'x'); os.fsync(f.fileno()); time.sleep(.02)"
        )
        parent = (
            "import subprocess,time; subprocess.Popen(['python','-c',"
            + repr(child)
            + "],start_new_session=True); time.sleep(300)"
        )
        jobs.launch(
            "detached-writer",
            workspace,
            root / "state",
            root / "generated",
            ["python", "-c", parent],
            {},
        )
        deadline = time.monotonic() + 5
        while not target.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not target.exists() or jobs.quiescence("detached-writer")["writers_stopped"]:
            raise RuntimeError("Detached writer failed to start or received premature stop proof")
        checkpoint(root / "launch.json", {"image_id": runtime.image_id, "writer_started": True})
        # Exit this process entirely. Recovery has no in-memory launcher state.
        return
    if not target.exists():
        raise RuntimeError("No detached writer from previous service process")
    before = target.stat().st_size
    time.sleep(0.15)
    if target.stat().st_size <= before:
        raise RuntimeError("Writer did not survive service process exit")
    if jobs.quiescence("detached-writer")["writers_stopped"]:
        raise RuntimeError("Restart incorrectly declared a live writer stopped")
    jobs.stop("detached-writer", force=True)
    deadline = time.monotonic() + 5
    while True:
        proof = jobs.quiescence("detached-writer")
        if proof["writers_stopped"]:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("All writers did not stop within the gate budget")
        time.sleep(0.05)
    validate_quiescence(proof, "detached-writer", config.backend_id)
    final_size = target.stat().st_size
    time.sleep(0.15)
    if target.stat().st_size != final_size:
        raise RuntimeError("Writer continued after quiescence proof")
    result = {
        "passed": True,
        "gate": "contained-writer-lifecycle",
        "gpu_acceptance": False,
        "service_process_restart": True,
        "detached_writer_survived_restart": True,
        "writer_stopped_after_cancel": True,
        "image_id": runtime.image_id,
        "proof": proof,
    }
    checkpoint(root / "result.json", result)


if __name__ == "__main__":
    main()
