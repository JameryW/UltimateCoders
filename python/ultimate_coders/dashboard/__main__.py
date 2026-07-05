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
import asyncio
import logging
import os
import signal
import sys
from typing import Any

logger = logging.getLogger("ultimate_coders.dashboard.__main__")

_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
_NATS_CONNECT_TIMEOUT = 2.0  # seconds — short, best-effort


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


def _connect_nats(nats_url: str) -> Any | None:
    """Best-effort synchronous NATS connect.

    ``nats.connect()`` is lazy and, on connection failure, will retry
    indefinitely by default. We disable reconnects and hard-cap the
    whole call with ``asyncio.wait_for`` so a dead NATS server cannot
    block startup. Returns the connected client, or None on any failure.
    """
    try:
        import nats as nats_lib
    except ImportError:
        logger.warning(
            "nats-py not installed; dashboard running without NATS. "
            "Install with: pip install nats-py"
        )
        return None

    loop = asyncio.new_event_loop()
    try:
        # max_reconnect_attempts=0 prevents the internal reconnect loop
        # from blocking when the server is unreachable.
        client = loop.run_until_complete(
            asyncio.wait_for(
                nats_lib.connect(
                    nats_url,
                    connect_timeout=_NATS_CONNECT_TIMEOUT,
                    max_reconnect_attempts=0,
                ),
                timeout=_NATS_CONNECT_TIMEOUT + 1.0,
            )
        )
        # Force a real round-trip so lazy-connect failures surface now.
        loop.run_until_complete(asyncio.wait_for(client.flush(), timeout=_NATS_CONNECT_TIMEOUT))
        return client
    except Exception as e:
        logger.warning(
            "Failed to connect to NATS at %s: %s. "
            "Dashboard running without NATS event subscription.",
            nats_url,
            f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
        )
        return None
    finally:
        loop.close()


def _drain_nats(nats_client: Any) -> None:
    """Best-effort NATS drain + close on shutdown."""
    if nats_client is None:
        return
    loop = asyncio.new_event_loop()
    try:
        try:
            loop.run_until_complete(nats_client.drain())
        except Exception as e:
            logger.debug("NATS drain failed (ignoring): %s", e)
    finally:
        loop.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Resolve NATS client
    if args.no_nats:
        nats_client: Any | None = None
        logger.info("--no-nats: skipping NATS connection")
    else:
        nats_client = _connect_nats(args.nats_url)
        if nats_client is not None:
            logger.info("Connected to NATS at %s", args.nats_url)

    # Import here so a missing nats-py / maturin build doesn't break --help
    from ultimate_coders.dashboard.app import DashboardApp

    app = DashboardApp(orchestrator=None, nats_client=nats_client)
    app.start(host=args.host, port=args.port)

    print(
        f"Dashboard API: http://localhost:{args.port}/dashboard/\n"
        f"Dashboard UI:  http://localhost:5173  (Vite dev; run `cd dashboard && bun run dev`)\n"
        f"NATS: {'connected' if nats_client is not None else 'not connected (SSE snapshot-only)'}",
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
        _drain_nats(nats_client)
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
