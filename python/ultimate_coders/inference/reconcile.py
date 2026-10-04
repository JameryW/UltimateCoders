"""Evidence-backed operator recovery with compare-and-set and audit."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
from dataclasses import dataclass

import httpx

from ultimate_coders.runtime_state import RuntimeState, process_matches

from .adapter import MetaInferAdapter
from .resources import ResourceBudget
from .service_contract import CONTRACT_VERSION


@dataclass(frozen=True)
class RemoteJob:
    operation_id: str
    version: int
    state: str
    data: dict


class RemoteJobs:
    def __init__(self, state: RuntimeState, url: str):
        self.state, self.url = state, url

    def get(self, operation_id: str) -> RemoteJob:
        record = self.state.versioned_get("remote_jobs", operation_id)
        if not record:
            raise ValueError("Remote operation not found")
        return RemoteJob(operation_id, record.version, record.data.get("state", ""), record.data)

    async def reconcile(
        self,
        operation_id: str,
        expected_version: int,
        action: str,
        *,
        actor: str,
        remote_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict:
        job = await asyncio.to_thread(self.get, operation_id)
        if job.version != expected_version:
            from ultimate_coders.runtime_state import RecordConflictError

            raise RecordConflictError("Record version changed")
        if not actor or len(actor) > 128:
            raise ValueError("Operator identity is required")
        backend_id = job.data.get("backend_id") or hashlib.sha256(self.url.encode()).hexdigest()
        adapter = MetaInferAdapter(self.url, backend_id=backend_id)

        async def apply(http):
            if action == "attach":
                if job.state not in ("submission_unknown", "cleanup_pending") or not remote_id:
                    raise ValueError("Attach requires an uncertain submission and remote task ID")
                if not remote_id.replace("-", "").replace("_", "").isalnum():
                    raise ValueError("Invalid remote task ID")
                proof = await adapter._request(http, "GET", f"/api/uc/jobs/{remote_id}/identity")
                if (
                    proof.get("contract_version") != CONTRACT_VERSION
                    or proof.get("task_id") != remote_id
                    or proof.get("backend_id") != backend_id
                    or proof.get("operation_id") != operation_id
                    or proof.get("request_sha256") != job.data.get("request_sha256")
                ):
                    raise ValueError("Remote identity does not match the persisted submission")
                values = {"state": "running", "remote_id": remote_id}
            elif action == "confirm_stop":
                if job.state not in ("cleanup_pending", "running", "stopped"):
                    raise ValueError("Stop reconciliation requires a known remote writer")
                identity = job.data.get("remote_id")
                if not identity:
                    raise ValueError("Attach the uncertain remote task before confirming stop")
                proof = await adapter.confirm_quiescence(http, identity)
                values = {"state": "stopped", "quiescence": proof}
            else:
                raise ValueError("Unknown reconciliation action")
            result = await asyncio.to_thread(
                self.state.mutate,
                "remote_jobs",
                operation_id,
                lambda old: {**old, **values},
                expected_version=expected_version,
                audit={"actor": actor, "action": action, "evidence": proof},
            )
            if action == "confirm_stop":
                await asyncio.to_thread(
                    ResourceBudget(self.state, backend_id).release, operation_id
                )
                for slot in await asyncio.to_thread(self.state.records, "backend_slots"):
                    if slot.get("operation_id") == operation_id:
                        await asyncio.to_thread(
                            self.state.mutate,
                            "backend_slots",
                            slot["key"],
                            lambda old: {} if old.get("operation_id") == operation_id else old,
                        )
            return result

        if client is not None:
            return await apply(client)
        async with httpx.AsyncClient(follow_redirects=False) as http:
            return await apply(http)

    def recover_workspace(self, lease_key: str, expected_version: int, *, actor: str) -> dict:
        lease = self.state.versioned_get("workspace_leases", lease_key)
        if not lease or not actor:
            raise ValueError("Workspace lease and operator identity are required")
        if lease.version != expected_version:
            from ultimate_coders.runtime_state import RecordConflictError

            raise RecordConflictError("Workspace lease version changed")
        path = lease.data.get("handle", {}).get("worktree_path")
        experiments, after = [], ""
        while True:
            page = self.state.query("experiments", workspace=path, after=after)
            experiments.extend(page)
            if len(page) < 100:
                break
            after = page[-1]["key"]
        for experiment in experiments:
            after = ""
            while True:
                page = self.state.query("remote_jobs", prefix=experiment["key"] + ":", after=after)
                if any(
                    job.get("state") not in ("completed", "stopped", "waiting_resource")
                    for job in page
                ):
                    raise ValueError("Workspace has unresolved remote execution")
                if len(page) < 100:
                    break
                after = page[-1]["key"]

        def recover(old):
            if old.get("host") != socket.gethostname():
                raise ValueError("Run workspace reconciliation on the owning worker host")
            for pid_key, birth_key in (
                ("pid", "process_identity"),
                ("runner_pid", "runner_process_identity"),
            ):
                if old.get(pid_key) and process_matches(old[pid_key], old.get(birth_key)):
                    raise ValueError("Workspace still has a live local owner")
            handle = old.get("handle", {})
            return {**old, "status": "reconciled", "handle": {**handle, "status": "reconciled"}}

        result = self.state.mutate(
            "workspace_leases",
            lease_key,
            recover,
            expected_version=expected_version,
            audit={
                "actor": actor,
                "action": "recover_workspace",
                "evidence": "local host/process birth and remote terminal states",
            },
        )
        for experiment in experiments:
            for resource_id in experiment.get("gpu_reservations", []):
                ResourceBudget(self.state, resource_id).release(experiment["key"])
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("inspect", "attach", "confirm_stop", "recover_workspace")
    )
    parser.add_argument("key")
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--remote-id")
    parser.add_argument("--actor", default=os.environ.get("USERNAME") or os.environ.get("USER"))
    args = parser.parse_args()
    state = RuntimeState()
    jobs = RemoteJobs(state, os.environ.get("UC_METAINFER_URL", ""))
    if args.action == "inspect":
        record = state.versioned_get("remote_jobs", args.key)
        print(
            json.dumps(
                {"version": record.version, "job": record.data} if record else None, indent=2
            )
        )
        return
    if args.expected_version is None:
        parser.error("--expected-version is required for a mutation")
    if args.action == "recover_workspace":
        result = jobs.recover_workspace(args.key, args.expected_version, actor=args.actor)
    else:
        result = asyncio.run(
            jobs.reconcile(
                args.key,
                args.expected_version,
                args.action,
                actor=args.actor,
                remote_id=args.remote_id,
            )
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
