"""Explicit local Linux service entry point; isolated job image is required."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from .app import ServiceConfig
from .oci import LocalOCI
from .upstream import PINNED_REVISION, create_service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, action="append", required=True)
    parser.add_argument("--backend-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", choices=("podman", "docker"), default="podman")
    parser.add_argument("--gpu", choices=("none", "wsl", "nvidia"), default="none")
    parser.add_argument(
        "--attest-ollama",
        action="store_true",
        help="Probe the explicit native Ollama profile inside the GPU image before serving",
    )
    parser.add_argument(
        "--ollama-observe-url", help="Explicit host-side address of the same pinned Ollama model"
    )
    parser.add_argument(
        "--job-env",
        type=Path,
        help="JSON object of explicit job environment; never inherit host secrets",
    )
    parser.add_argument("--port", type=int, default=48766)
    args = parser.parse_args()
    environment = json.loads(args.job_env.read_text()) if args.job_env else {}
    if not isinstance(environment, dict) or any(
        not isinstance(v, str) for v in environment.values()
    ):
        parser.error("Job environment must be a JSON object of string values")
    if not args.backend_id.strip():
        parser.error("Backend identity is required")
    config = ServiceConfig(
        args.control.resolve(), tuple(args.workspace_root), args.backend_id, PINNED_REVISION
    )
    runtime = LocalOCI(args.runtime, args.image, gpu=args.gpu)
    app = create_service(
        args.source,
        args.root,
        config,
        runtime,
        environment,
        attest_ollama=args.attest_ollama,
        ollama_observe_url=args.ollama_observe_url,
    )
    # External deployments expose this through an authenticated private proxy.
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
