"""Durable OCI job ownership. Only observed kernel scope emptiness permits cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

from ultimate_coders.inference.service_contract import CONTRACT_VERSION
from ultimate_coders.runtime_state import RuntimeState, atomic_json

from .app import ServiceConfig


class ContainerRuntime(Protocol):
    image_id: str

    def validate(self, mounts: list, command: list, environment: dict) -> None: ...

    def create(
        self, name: str, labels: dict, mounts: list, command: list, environment: dict
    ) -> str: ...
    def start(self, container_id: str) -> None: ...
    def inspect(self, container_id: str) -> dict: ...
    def capture_scope(self, pid: int, container_id: str) -> str: ...
    def stop(self, container_id: str, force: bool) -> None: ...


def checkpoint(path: Path, data: dict) -> None:
    atomic_json(path, data)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    # Directory fsync is required for Linux service crash recovery. Windows only
    # runs contract fixtures; production execution is restricted to local Linux.
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ContainedJobs:
    workspace_probe_identity = True

    def __init__(
        self,
        config: ServiceConfig,
        runtime: ContainerRuntime,
        *,
        boot_id: str | None = None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ):
        self.config, self.runtime = config, runtime
        self.boot_id = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.cgroup_root = cgroup_root.resolve(strict=True)
        config.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = config.control_dir / "identity.json"
        if not identity.exists():
            # A directory claim prevents concurrent service initializers from
            # silently assigning different resource identities to the same store.
            claim = config.control_dir / "identity-claim"
            claim.mkdir()
            checkpoint(identity, {"backend_id": config.backend_id})
        if json.loads(identity.read_text())["backend_id"] != config.backend_id:
            raise ValueError("Control store belongs to a different backend")
        self.records = config.control_dir / "jobs"
        self.records.mkdir(exist_ok=True)
        self.evidence = config.control_dir / "evidence"
        self.evidence.mkdir(exist_ok=True)
        self.probe_claims = config.control_dir / "probe_claims"
        self.probe_claims.mkdir(exist_ok=True)
        sync_directory(config.control_dir)
        self.ownership = RuntimeState(config.control_dir / "ownership.sqlite3", database_url="")
        self.hardware_attestation = None

    def _path(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
            raise ValueError("Invalid task identity")
        return self.records / (hashlib.sha256(task_id.encode()).hexdigest() + ".json")

    def _load(self, task_id: str) -> dict:
        record = json.loads(self._path(task_id).read_text())
        if (
            record["task_id"] != task_id
            or record["backend_id"] != self.config.backend_id
            or record["boot_id"] != self.boot_id
        ):
            raise RuntimeError("Job identity or host boot changed; scope requires reconciliation")
        return record

    def _observe(self, record: dict) -> dict:
        observed = self.runtime.inspect(record.get("container_id") or record["name"])
        if (
            any(observed["labels"].get(k) != v for k, v in record["labels"].items())
            or observed["image"] != record["image_id"]
            or observed.get("isolation") is not True
            or observed.get("restart_count") != 0
            or (record.get("generation") and observed.get("generation") != record["generation"])
            or (record.get("container_id") and observed["id"] != record["container_id"])
        ):
            raise RuntimeError("Container identity/isolation changed; writers remain unknown")
        return observed

    def launch(
        self,
        task_id: str,
        workspace: Path,
        state_dir: Path,
        generated: Path,
        command: list[str],
        environment: dict[str, str],
        *,
        extra_mounts: list[tuple[Path, bool]] | None = None,
    ) -> int:
        if self.hardware_attestation is not None:
            from .ollama import OllamaProfile

            if any(
                environment.get(key) != value
                for key, value in self.hardware_attestation.environment.items()
            ) or OllamaProfile.from_environment(environment) != self.hardware_attestation.profile:
                raise ValueError("Job environment changed from the attested coding profile")
            self.hardware()
        workspace = self.config.workspace(str(workspace))
        state_dir, generated = state_dir.resolve(), generated.resolve()
        mounts = [(workspace, True), (state_dir, True), (generated, True)]
        mounts += extra_mounts or []
        control = self.config.control_dir.resolve()
        if any(
            path == control or path in control.parents or control in path.parents
            for path, _ in mounts
        ):
            raise ValueError("Control store must be outside all container mounts")
        # Pure runtime validation must precede the durable ownership claim.
        self.runtime.validate(mounts, command, environment)
        for path in (state_dir, generated):
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                owner = workspace.stat()
                os.chown(path, owner.st_uid, owner.st_gid)
        requirements = state_dir / "requirements.json"
        if os.name == "posix" and requirements.is_file():
            os.chown(requirements, owner.st_uid, owner.st_gid, follow_symlinks=False)
        gate = state_dir / ".uc-launch-ready"
        if gate.exists():
            raise ValueError("Launch directory contains a previous gate")
        key = hashlib.sha256((self.config.backend_id + ":" + task_id).encode()).hexdigest()
        labels = {
            "uc.backend": self.config.backend_id,
            "uc.task": task_id,
            "uc.revision": self.config.revision,
            "uc.request": hashlib.sha256(
                json.dumps(
                    [command, environment, [(str(p), rw) for p, rw in mounts]], sort_keys=True
                ).encode()
            ).hexdigest(),
        }
        record: dict[str, Any] = {
            "task_id": task_id,
            "backend_id": self.config.backend_id,
            "boot_id": self.boot_id,
            "name": "uc-mi-" + key[:40],
            "labels": labels,
            "image_id": self.runtime.image_id,
            "started_at": time.time(),
            "gate_released": False,
            "workspace": str(workspace),
        }
        path = self._path(task_id)

        def claim(old):
            if old.get("task_id"):
                raise FileExistsError("Workspace is owned by another unresolved launch")
            return {"task_id": task_id, "workspace": str(workspace)}

        self.ownership.mutate("workspace_owners", str(workspace), claim)
        # Never replay a create or start, even if their response was lost.
        try:
            with path.open("x", encoding="utf-8") as writer:
                json.dump(record, writer)
                writer.flush()
                os.fsync(writer.fileno())
        except FileExistsError:
            # A duplicate identity failed before runtime.create was invoked.
            # Release only this never-launched claim, not the old job's scope.
            self.ownership.mutate(
                "workspace_owners",
                str(workspace),
                lambda old: {} if old.get("task_id") == task_id else old,
            )
            raise
        sync_directory(path.parent)
        bootstrap = [
            "/bin/sh",
            "-c",
            'while [ ! -f "$1" ]; do sleep 0.05; done; shift; log="$1"; shift; '
            'exec "$@" >> "$log" 2>&1',
            "uc-launch",
            str(gate),
            str(state_dir / "orchestrator.log"),
            *command,
        ]
        record["container_id"] = self.runtime.create(
            record["name"], labels, mounts, bootstrap, environment
        )
        checkpoint(path, record)
        self.runtime.start(record["container_id"])
        observed = self._observe(record)
        if not observed["running"] or observed["pid"] <= 0:
            raise RuntimeError("Container did not reach the launch gate")
        record["cgroup"] = self.runtime.capture_scope(observed["pid"], observed["id"])
        record["pid"] = observed["pid"]
        record["generation"] = observed.get("generation")
        checkpoint(path, record)
        # The scope is durable BEFORE untrusted code can execute.
        checkpoint(gate, {"task_id": task_id})
        record["gate_released"] = True
        checkpoint(path, record)
        return observed["pid"]

    def status(self, task_id: str) -> dict:
        record = self._load(task_id)
        observed = self._observe(record)
        return {
            "running": observed["running"],
            "pid": observed["pid"] or record.get("pid"),
            "started_at": record["started_at"],
            "finished_at": None if observed["running"] else observed.get("finished_at"),
            "exit_hint": "container-running" if observed["running"] else "container-stopped",
        }

    def stop(self, task_id: str, force: bool = False) -> bool:
        record = self._load(task_id)
        observed = self._observe(record)
        # Recover an interrupted gated start by observing its immutable identity;
        # never send a second start. Missing scopes cannot produce a stop receipt.
        if observed["running"] and not record.get("cgroup"):
            record["container_id"] = observed["id"]
            record["cgroup"] = self.runtime.capture_scope(observed["pid"], observed["id"])
            record["pid"] = observed["pid"]
            record["generation"] = observed.get("generation")
            checkpoint(self._path(task_id), record)
        if observed["running"]:
            try:
                self.runtime.stop(observed["id"], force)
            except RuntimeError:
                # A short-lived process may exit between inspect and kill.
                # Only a fresh, identity-checked stopped observation can turn
                # that race into an acknowledgement; it is still not a proof.
                if self._observe(record)["running"]:
                    raise
        return True  # Request acknowledgement; quiescence is a separate observation.

    def quiescence(self, task_id: str) -> dict:
        if re.fullmatch(r"probe-[a-f0-9]{32}", task_id) and not self._path(task_id).exists():
            return self._probe_fence(task_id)
        record = self._load(task_id)
        observed = self._observe(record)
        if not record.get("cgroup"):
            raise RuntimeError("No durable kernel scope; writers remain unknown")
        scope = Path(record["cgroup"]).resolve()
        if self.cgroup_root not in scope.parents:
            raise RuntimeError("Kernel scope is outside the configured cgroup tree")
        stopped = not observed["running"] and observed["pid"] == 0
        try:
            scope.stat()
        except FileNotFoundError:
            scope_missing = True
        else:
            scope_missing = False
        if not scope_missing:

            def unreadable(error):
                raise error

            # rglob can suppress permission errors; os.walk must propagate them.
            for directory, _, _ in os.walk(scope, onerror=unreadable):
                if (Path(directory) / "cgroup.procs").read_text().strip():
                    stopped = False
        proof = {
            "contract_version": CONTRACT_VERSION,
            "task_id": task_id,
            "backend_id": self.config.backend_id,
            "writers_stopped": stopped,
            "proof_kind": "cgroup_empty",
            "execution_scope": self.boot_id + ":" + observed["id"] + ":" + str(scope),
            "container_id": observed["id"],
            "cgroup": str(scope),
            "observed_at": time.time(),
        }
        proof["evidence_id"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True).encode()
        ).hexdigest()
        if stopped:
            checkpoint(self.evidence / (proof["evidence_id"] + ".json"), proof)
            if record.get("workspace"):
                # Proof is durable first. The transaction releases only this job;
                # repeated observations cannot clear a subsequent job's owner.
                self.ownership.mutate(
                    "workspace_owners",
                    record["workspace"],
                    lambda old: {} if old.get("task_id") == task_id else old,
                )
        return proof

    def _probe_fence(self, probe_id: str) -> dict:
        path = self.probe_claims / (probe_id + ".json")
        try:
            with path.open("x", encoding="utf-8") as writer:
                json.dump({"task_id": probe_id, "state": "fenced"}, writer)
                writer.flush()
                os.fsync(writer.fileno())
            sync_directory(self.probe_claims)
        except FileExistsError:
            pass  # The exclusive claim decides whether a launch may still proceed.
        with path.open("r+", encoding="utf-8") as reader:
            claim = json.load(reader)
            # A concurrent observer may read complete JSON before its creator's
            # fsync. Every receipt publisher makes the fence durable itself.
            os.fsync(reader.fileno())
        sync_directory(self.probe_claims)
        if claim.get("state") != "fenced":
            return {
                "task_id": probe_id,
                "backend_id": self.config.backend_id,
                "writers_stopped": False,
            }
        proof = {
            "contract_version": CONTRACT_VERSION,
            "task_id": probe_id,
            "backend_id": self.config.backend_id,
            "writers_stopped": True,
            "proof_kind": "workspace_fenced",
            "execution_scope": self.boot_id + ":probe-fence:" + probe_id,
            "observed_at": time.time(),
        }
        proof["evidence_id"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True).encode()
        ).hexdigest()
        checkpoint(self.evidence / (proof["evidence_id"] + ".json"), proof)
        return proof

    def hardware(self) -> dict:
        if self.hardware_attestation is None:
            raise RuntimeError("GPU/model runtime evidence has not been configured")
        return self.hardware_attestation.observe()

    def enable_hardware(
        self, environment: dict[str, str], *, observe_url: str | None = None
    ) -> None:
        from .attestation import HardwareAttestation

        attestation = HardwareAttestation(self, environment, observe_url=observe_url)
        attestation.initialize()
        self.hardware_attestation = attestation

    def workspace_probe(
        self,
        workspace: Path,
        source: Path,
        target: Path,
        token: str,
        *,
        probe_id: str | None = None,
    ) -> dict:
        import uuid

        if probe_id is None:
            probe_id = "probe-" + uuid.uuid4().hex
        if not re.fullmatch(r"probe-[a-f0-9]{32}", probe_id):
            raise ValueError("Invalid workspace probe identity")
        if self._path(probe_id).exists():
            raise FileExistsError("Workspace probe identity already exists")
        # Claim before the cleanup region: a duplicate request must never stop
        # another invocation's scope, even when create/start is still pending.
        with (self.probe_claims / (probe_id + ".json")).open("x", encoding="utf-8") as writer:
            json.dump(
                {"task_id": probe_id, "workspace": str(workspace), "state": "launching"}, writer
            )
            writer.flush()
            os.fsync(writer.fileno())
        sync_directory(self.probe_claims)
        root = (
            self.config.control_dir.parent / (self.config.control_dir.name + "-probes") / probe_id
        )
        script = (
            "import hashlib,json,os,socket; p=" + repr(str(target)) + "; "
            "digest=hashlib.sha256(open(" + repr(str(source)) + ", 'rb').read()).hexdigest(); "
            "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644); "
            "os.fchmod(fd,0o644); "
            "os.write(fd," + repr(token.encode()) + "); os.fsync(fd); os.close(fd); "
            "print(json.dumps({'read_sha256':digest,'writer_identity':socket.gethostname()+"
            "':'+str(os.getuid())+':'+str(os.getpid())}))"
        )
        try:
            self.launch(
                probe_id,
                workspace,
                root / "state",
                root / "generated",
                ["python", "-c", script],
                self.hardware_attestation.environment if self.hardware_attestation else {},
            )
            deadline = time.monotonic() + 3
            proof = self.quiescence(probe_id)
            while not proof["writers_stopped"]:
                if time.monotonic() > deadline:
                    raise RuntimeError("Workspace probe did not become quiescent")
                time.sleep(0.02)
                proof = self.quiescence(probe_id)
            result = json.loads((root / "state" / "orchestrator.log").read_text())
            return {**result, "quiescence": proof}
        except (OSError, ValueError, RuntimeError) as exc:
            if not self._path(probe_id).exists():
                # Launch intent is durable before OCI create. The exclusive
                # probe claim also prohibits a delayed/replayed launch.
                checkpoint(
                    self.probe_claims / (probe_id + ".json"),
                    {"task_id": probe_id, "state": "fenced"},
                )
                self._probe_fence(probe_id)
                raise RuntimeError(f"Workspace probe {probe_id} failed before OCI launch") from exc
            try:
                self.stop(probe_id, force=True)
                deadline = time.monotonic() + 3
                while not self.quiescence(probe_id)["writers_stopped"]:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Probe writers remain unresolved")
                    time.sleep(0.02)
            except (OSError, ValueError, RuntimeError) as cleanup_exc:
                raise RuntimeError(
                    f"Workspace probe {probe_id} failed; reconcile its writer scope"
                ) from cleanup_exc
            raise RuntimeError(f"Workspace probe {probe_id} failed after verified stop") from exc
