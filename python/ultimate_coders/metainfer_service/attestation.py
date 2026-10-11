"""Startup evidence from the owned image, with live GPU/model drift checks."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from ultimate_coders.inference.service_contract import CONTRACT_VERSION, validate_quiescence

from .app import ServiceConfig
from .observations import read_json
from .ollama import CODING_CLIENT, OllamaProfile, native_client_identity


class HardwareAttestation:
    def __init__(self, jobs, environment: dict[str, str], *, observe_url: str | None = None):
        if (
            getattr(jobs.runtime, "image_labels", {}).get("uc.metainfer.ollama") != "1"
            or getattr(jobs.runtime, "gpu", "none") not in ("wsl", "nvidia")
            or environment.get("METAINFER_AGENT_BACKEND") != "codex"
            or environment.get("METAINFER_CODEX_BIN") != CODING_CLIENT
            or set(environment)
            - {
                "METAINFER_AGENT_BACKEND",
                "METAINFER_CODEX_BIN",
                "UC_OLLAMA_URL",
                "UC_OLLAMA_MODEL",
                "UC_OLLAMA_DIGEST",
                "METAINFER_ROOT",
                "METAINFER_NODE_ID",
                "HOME",
                "GIT_OPTIONAL_LOCKS",
                "PYTHONDONTWRITEBYTECODE",
                "TRITON_CACHE_DIR",
                "TORCHINDUCTOR_CACHE_DIR",
                "CODEX_HOME",
            }
        ):
            raise ValueError("Hardware profile must bind the GPU image and UC native coding client")
        self.profile = OllamaProfile.from_environment(environment)
        self.observer = OllamaProfile(
            observe_url or self.profile.url, self.profile.model, self.profile.digest
        )
        self.jobs = jobs
        self.environment = dict(environment)
        self.report: dict = {}
        self.profile_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "environment": self.environment,
                    "observer": self.observer.url,
                    "gpu_mode": jobs.runtime.gpu,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def initialize(self, *, timeout: float = 210) -> None:
        from .jobs import ContainedJobs, checkpoint

        cached = read_json(self.jobs.config.control_dir, "hardware.json", None)
        if cached is not None:
            if not isinstance(cached, dict):
                raise ValueError("Invalid private hardware cache")
            digest = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in cached.items() if key != "evidence_id"},
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if cached.get("evidence_id") != digest:
                raise ValueError("Hardware cache checksum mismatch")
            if (
                cached.get("boot_id") == self.jobs.boot_id
                and cached.get("image_id") == self.jobs.runtime.image_id
                and cached.get("profile_sha256") == self.profile_sha256
                and cached.get("runtime", {}).get("image_code") == native_client_identity()
                and cached.get("backend_id") == self.jobs.config.backend_id
                and cached.get("revision") == self.jobs.config.revision
            ):
                proof = cached.get("stop_proof", {})
                validate_quiescence(proof, proof.get("task_id", ""), self.jobs.config.backend_id)
                if self.jobs.quiescence(proof["task_id"])["writers_stopped"] is not True:
                    raise RuntimeError("Cached hardware probe writers remain unresolved")
                self.report = cached
                self.observe()
                return
        if self.jobs.ownership.query("workspace_owners", task_id_present=True, limit=1):
            raise RuntimeError("Quiesce all owned jobs before changing the hardware profile")
        task = "hardware-" + uuid.uuid4().hex
        root = (
            self.jobs.config.control_dir.parent
            / (self.jobs.config.control_dir.name + "-hardware")
            / task
        )
        workspace = root / "workspace"
        workspace.mkdir(parents=True, mode=0o700)
        if os.getuid() == 0:
            os.chown(workspace, 65534, 65534)
        config = ServiceConfig(
            self.jobs.config.control_dir,
            (workspace,),
            self.jobs.config.backend_id,
            self.jobs.config.revision,
        )
        probe = ContainedJobs(
            config, self.jobs.runtime, boot_id=self.jobs.boot_id, cgroup_root=self.jobs.cgroup_root
        )
        state, generated = root / "state", root / "generated"
        proof = None
        try:
            probe.launch(
                task,
                workspace,
                state,
                generated,
                [
                    "python",
                    "-m",
                    "ultimate_coders.metainfer_service.gpu_probe",
                    "--output",
                    str(state / "hardware.json"),
                ],
                {
                    **self.environment,
                    "HOME": "/tmp",
                    "TRITON_CACHE_DIR": "/tmp/triton",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            deadline = time.monotonic() + timeout
            while probe.status(task)["running"]:
                if time.monotonic() > deadline:
                    raise TimeoutError("Hardware startup probe deadline exceeded")
                time.sleep(0.1)
        finally:
            # An absent/uncertain record also fails here, preserving quarantine;
            # it cannot turn a failed launch into accepted hardware evidence.
            probe.stop(task, force=True)
            proof = probe.quiescence(task)
            if proof["writers_stopped"] is not True:
                raise RuntimeError(f"Hardware probe {task} requires writer reconciliation")
        report = read_json(state, "hardware.json", None)
        expected = native_client_identity()
        if (
            not isinstance(report, dict)
            or report.get("runtime", {}).get("image_code") != expected
            or report.get("model_identity", {}).get("digest") != self.profile.digest
            or report.get("model_identity", {}).get("model") != self.profile.model
            or not report.get("model_probe")
            or len(report.get("devices", [])) != 1
            or not report.get("gpu_cases")
        ):
            raise ValueError(f"Hardware probe {task} returned no matching GPU/model evidence")
        self.report = {
            **report,
            "contract_version": CONTRACT_VERSION,
            "backend_id": config.backend_id,
            "revision": config.revision,
            "image_id": self.jobs.runtime.image_id,
            "boot_id": self.jobs.boot_id,
            "stop_proof": proof,
            "profile_sha256": self.profile_sha256,
        }
        self.report["evidence_id"] = hashlib.sha256(
            json.dumps(self.report, sort_keys=True).encode()
        ).hexdigest()
        checkpoint(self.jobs.evidence / (self.report["evidence_id"] + ".json"), self.report)
        self.observe()
        checkpoint(self.jobs.config.control_dir / "hardware.json", self.report)

    def observe(self) -> dict:
        if (
            not self.report
            or Path("/proc/sys/kernel/random/boot_id").read_text().strip() != self.jobs.boot_id
        ):
            raise RuntimeError("Hardware evidence is unavailable or host boot changed")
        try:
            observed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=uuid,name,driver_version,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("GPU identity observation timed out") from exc
        if observed.returncode:
            raise RuntimeError("GPU identity observation failed")
        output = observed.stdout
        devices = [
            dict(zip(("uuid", "model", "driver", "memory_mib"), (part.strip() for part in row)))
            for row in csv.reader(output.splitlines())
        ]
        if devices != self.report["devices"] or self.observer.identity() != {
            key: self.report["model_identity"][key] for key in ("provider", "model", "digest")
        }:
            raise RuntimeError("GPU or Ollama identity changed; re-attestation is required")
        return json.loads(json.dumps(self.report))
