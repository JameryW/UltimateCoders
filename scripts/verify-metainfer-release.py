"""Run the UC MetaInfer deployment gate and write portable evidence.

The gate intentionally stops before creating a remote job. It proves that the
configured service is the pinned deployment, shares the assigned workspace,
and (for GPU releases) attests the actual device/runtime/model identity. The
workflow-specific acceptance/cancel/restart job is a separate self-hosted GPU
step because it must execute the real pinned service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from ultimate_coders.inference.service_contract import verify_hardware, verify_workspace


async def verify(args: argparse.Namespace) -> dict:
    expected_revision = args.revision or os.environ.get("UC_METAINFER_REVISION")
    url = args.url.rstrip("/")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        contract_response = await client.get(url + "/api/uc/contract", timeout=5)
        contract_response.raise_for_status()
        contract = contract_response.json()
        workspace = await verify_workspace(
            client, url, args.workspace, expected_revision=expected_revision
        )
        hardware = None
        if args.require_gpu:
            hardware = await verify_hardware(
                client,
                url,
                contract["backend_id"],
                expected_revision=expected_revision,
                expected_model=args.model,
            )
    return {
        "contract": {
            "version": contract.get("contract_version"),
            "revision": contract.get("revision"),
            "backend_id": contract.get("backend_id"),
            "capabilities": contract.get("capabilities", []),
        },
        "workspace": workspace["workspace_receipt"],
        "hardware": hardware,
        "gpu_required": args.require_gpu,
        "service_url": url,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("UC_METAINFER_URL"), required=False)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    if not args.url:
        parser.error("--url or UC_METAINFER_URL is required")
    evidence = asyncio.run(verify(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
