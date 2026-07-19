"""CLI entry point: ``python -m ultimate_coders.dashboard``.

Starts the FastAPI dashboard backend without requiring an Orchestrator
instance. The dashboard serves REST/SSE on the given port; the frontend
(Vite dev :5173) and gRPC-Web (:50051) talk to it via the Vite proxy.

NATS connection is best-effort: when the server is unreachable the
dashboard still starts, but the SSE ``/stream`` will not carry
``uc.task.event`` events (it falls back to the 5s full-snapshot push).
This is important for the non-docker ``run-omp.sh`` mode, where no
NATS broker is running.

Usage::

    python -m ultimate_coders.dashboard [--host HOST] [--port PORT]
        [--nats-url URL | --no-nats]

Environment:
    UC_NATS_URL   NATS URL (default: nats://127.0.0.1:4222).
                  Overridden by --nats-url. Ignored when --no-nats is set.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger("ultimate_coders.dashboard.__main__")

_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env_nats = os.environ.get("UC_NATS_URL", _DEFAULT_NATS_URL)
    parser = argparse.ArgumentParser(
        prog="python -m ultimate_coders.dashboard",
        description="Start the UltimateCoders dashboard backend (no Orchestrator required).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument(
        "--nats-url",
        default=env_nats,
        help=f"NATS server URL (default: $UC_NATS_URL or {_DEFAULT_NATS_URL})",
    )
    parser.add_argument(
        "--no-nats",
        action="store_true",
        help="Skip NATS connection entirely (SSE will not carry uc.task.event).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # ponytail: F58 — pass the NATS URL; don't pre-connect. The old code
    # connected on a throwaway event loop (this thread has no running loop)
    # and closed it immediately — the client's reader/ping tasks died with
    # it, so the dashboard never received a single uc.task.event while
    # logging "Connected to NATS". DashboardApp connects on the uvicorn
    # server's event loop in its startup hook instead.
    if args.no_nats:
        nats_url: str | None = None
        logger.info("--no-nats: skipping NATS connection")
    else:
        nats_url = args.nats_url

    # Import here so a missing nats-py / maturin build doesn't break --help
    from ultimate_coders.dashboard.app import DashboardApp

    app = DashboardApp(orchestrator=None, nats_url=nats_url)
    app.start(host=args.host, port=args.port)

    print(
        f"Dashboard API: http://localhost:{args.port}/dashboard/\n"
        f"Dashboard UI:  http://localhost:5173  (Vite dev; run `cd dashboard && bun run dev`)\n"
        f"NATS: {'enabled (connects on server start — see log)' if nats_url else 'disabled (snapshot-only)'}",
        flush=True,
    )

    # Block the main thread until a signal is received so the daemon
    # uvicorn thread keeps running. signal.pause() returns after the
    # handler runs; on platforms without it, poll a short sleep.
    received_signal = {"signo": None}

    def _handle_signal(signo: int, _frame: Any) -> None:
        if received_signal["signo"] is not None:
            # Second signal — force exit
            logger.warning("Second signal %d received, forcing exit", signo)
            os._exit(1)
        received_signal["signo"] = signo
        logger.info("Signal %d received, shutting down dashboard...", signo)
        try:
            app.stop()
        except Exception:
            logger.warning("Error stopping dashboard", exc_info=True)
        # ponytail: F58 — NATS drain happens in the app's shutdown hook (on
        # the server's loop, where the client is bound).
        logger.info("Shutdown complete")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if hasattr(signal, "pause"):
            # Returns after a signal is handled; the handler shut us down.
            signal.pause()
        else:
            while received_signal["signo"] is None:
                import time

                time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        if received_signal["signo"] is None:
            _handle_signal(signal.SIGINT, None)

    # Handler already stopped the server + drained NATS. Give the daemon
    # thread a moment to finish so the process can exit cleanly.
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("UC_DASHBOARD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main())
