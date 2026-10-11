"""UC service extension: filesystem sharing and all-writer stop evidence."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

import httpx

CONTRACT_VERSION = "uc-metainfer/1"


class DeploymentContractError(RuntimeError):
    retryable = False


def validate_kernel_publication(repository: str, kernel_path: str, run: dict) -> dict:
    """Bind terminal publication evidence to the stopped worktree's actual bytes."""
    try:
        root, kernel = Path(repository).resolve(strict=True), Path(kernel_path)
        relative = kernel.relative_to(root)
        if (
            not kernel.is_absolute()
            or kernel.resolve(strict=True) != kernel
            or any(part in ("..", ".git") for part in relative.parts)
        ):
            raise ValueError("Unsafe kernel path")
        receipt = run.get("uc_candidate", {})
        if (
            run.get("finished") is not True
            or run.get("final_status") not in ("completed", "success", "succeeded")
            or receipt.get("publication_kind") != "oci_generated_candidate"
            or receipt.get("oracle_acceptance") != "pending"
            or receipt.get("kernel_path") != relative.as_posix()
            or not isinstance(receipt.get("candidate_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", receipt["candidate_id"])
        ):
            raise ValueError("Invalid publication receipt")
        descriptor = os.open(
            kernel, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as reader:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise ValueError("Kernel must be regular")
            content = reader.read(4 * 1024 * 1024 + 1)
        if (
            len(content) > 4 * 1024 * 1024
            or receipt.get("code_sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("Published kernel checksum mismatch")
        return receipt
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise DeploymentContractError("Kernel publication evidence is incomplete") from exc


def validate_quiescence(proof: dict, remote_id: str, backend_id: str) -> dict:
    if (
        proof.get("contract_version") != CONTRACT_VERSION
        or proof.get("task_id") != remote_id
        or proof.get("backend_id") != backend_id
        or proof.get("writers_stopped") is not True
        or proof.get("proof_kind") not in ("cgroup_empty", "workspace_fenced")
        or not isinstance(proof.get("execution_scope"), str)
        or not proof["execution_scope"]
        or not isinstance(proof.get("evidence_id"), str)
        or not proof["evidence_id"]
    ):
        raise DeploymentContractError("Service has not proved all workspace writers quiescent")
    return proof


async def verify_workspace(
    client: httpx.AsyncClient,
    url: str,
    workspace: str,
    *,
    expected_revision: str | None = None,
) -> dict:
    response = await client.get(url + "/api/uc/contract", timeout=5)
    if response.status_code == 404:
        raise DeploymentContractError(
            "Mutating MetaInfer jobs require the UC workspace/stop contract"
        )
    response.raise_for_status()
    contract = response.json()
    if (
        contract.get("contract_version") != CONTRACT_VERSION
        or not contract.get("backend_id")
        or not contract.get("revision")
        or not {"workspace_probe", "quiescence"}.issubset(contract.get("capabilities", []))
    ):
        raise DeploymentContractError("MetaInfer UC service contract is incompatible")
    if expected_revision and contract["revision"] != expected_revision:
        raise DeploymentContractError("MetaInfer service revision is not the configured pin")
    # Random disposable probes are removed before snapshotting or remote launch.
    root = Path(workspace).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".uc-probe-", dir=root) as directory:
        source, target = Path(directory) / "read", Path(directory) / "write"
        # Non-secret nonce challenges must work across Worker/job UIDs. The
        # sticky directory prevents peers from removing another UID's file;
        # random names and O_EXCL on the service still prevent clobbering.
        Path(directory).chmod(0o1777)
        source.write_bytes(uuid.uuid4().bytes)
        source.chmod(0o644)
        token = uuid.uuid4().hex
        response = await client.post(
            url + "/api/uc/workspaces/verify",
            timeout=5,
            json={
                "workspace": str(root),
                "read_path": str(source),
                "write_path": str(target),
                "write_token": token,
            },
        )
        response.raise_for_status()
        receipt = response.json()
        if (
            receipt.get("read_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()
            or not receipt.get("writer_identity")
            or not target.is_file()
            or target.read_text() != token
        ):
            raise DeploymentContractError("Worker and MetaInfer do not share a writable workspace")
        return {**contract, "workspace_receipt": receipt}


async def verify_hardware(
    client: httpx.AsyncClient,
    url: str,
    backend_id: str,
    *,
    expected_revision: str | None = None,
    expected_model: str | None = None,
) -> dict:
    """Require service-owned device/runtime identity before a GPU mutation."""
    response = await client.get(url + "/api/uc/hardware", timeout=5)
    if response.status_code == 404:
        raise DeploymentContractError("GPU tasks require the UC hardware identity contract")
    response.raise_for_status()
    evidence = response.json()
    if (
        evidence.get("contract_version") != CONTRACT_VERSION
        or evidence.get("backend_id") != backend_id
        or (expected_revision and evidence.get("revision") != expected_revision)
        or not evidence.get("devices")
        or not evidence.get("runtime")
        or not evidence.get("model_identity")
    ):
        raise DeploymentContractError("MetaInfer GPU identity evidence is incomplete")
    if expected_model and not any(
        expected_model.casefold() in str(device.get("model", "")).casefold()
        for device in evidence["devices"]
    ):
        raise DeploymentContractError("MetaInfer GPU model does not match the task")
    return evidence
