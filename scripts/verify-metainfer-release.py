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
import sys
from pathlib import Path

import httpx
from ultimate_coders.inference.service_contract import (
    DeploymentContractError,
    verify_hardware,
    verify_workspace,
)


async def verify(args: argparse.Namespace) -> dict:
    expected_revision = args.revision or os.environ.get("UC_METAINFER_REVISION")
    url = args.url.rstrip("/")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        # `verify_workspace` owns the contract fetch, and it is the only place
        # that turns "the service has no UC contract" into a sentence an
        # operator can act on. Do not fetch the contract again here: the
        # duplicate `raise_for_status()` used to run first and surface a bare
        # `httpx.HTTPStatusError` traceback instead, which is exactly what
        # someone pointing this at stock upstream MetaInfer sees -- measured
        # 2026-10-04 against a service answering 404 for /api/uc/*.
        workspace = await verify_workspace(
            client, url, args.workspace, expected_revision=expected_revision
        )
        hardware = None
        if args.require_gpu:
            hardware = await verify_hardware(
                client,
                url,
                workspace["backend_id"],
                expected_revision=expected_revision,
                expected_model=args.model,
            )
    return {
        "contract": {
            "version": workspace.get("contract_version"),
            "revision": workspace.get("revision"),
            "backend_id": workspace.get("backend_id"),
            "capabilities": workspace.get("capabilities", []),
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
    try:
        evidence = asyncio.run(verify(args))
    except DeploymentContractError as exc:
        # A service that lacks the contract, has the wrong revision, or cannot
        # prove it shares the workspace is an answer, not a crash. Exit non-zero
        # with the reason on one line so a release gate log records what was
        # rejected and why.
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"REJECTED: MetaInfer service is unreachable or unhealthy: {exc}", file=sys.stderr)
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
