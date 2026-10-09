"""Measure live NATS -> Dashboard API -> HTTP SSE latency.

Use an isolated broker/dashboard: this publishes synthetic uc.task.event messages.
The publisher and HTTP consumers share one monotonic clock. Browser rendering,
Worker execution and Gateway processing are outside the measured boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import nats


def summarize(samples: dict[int, float], count: int, duplicates: int, limit: float) -> dict:
    """Keep missing/duplicate events and slow samples visible in the verdict."""
    values = sorted(samples.values())

    def percentile(fraction: float) -> float | None:
        return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None

    return {
        "received": len(samples),
        "missing": [seq for seq in range(count) if seq not in samples],
        "duplicates": duplicates,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(values) if values else None,
        "latencies_ms": [samples.get(seq) for seq in range(count)],
        "passed": len(samples) == count
        and duplicates == 0
        and all(math.isfinite(value) and 0 <= value < limit for value in values),
    }


async def measure(args: argparse.Namespace) -> dict:
    run_id = uuid.uuid4().hex
    sent: dict[int, int] = {}
    samples: list[dict[int, float]] = [{} for _ in range(args.clients)]
    duplicates = [0] * args.clients
    connected = [asyncio.Event() for _ in range(args.clients)]
    ready = [asyncio.Event() for _ in range(args.clients)]
    complete = [asyncio.Event() for _ in range(args.clients)]
    errors: list[str] = []
    headers = {}
    token = os.environ.get(args.token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def consume(index: int, client: httpx.AsyncClient) -> None:
        try:
            async with client.stream(
                "GET", args.dashboard_url.rstrip("/") + "/dashboard/api/stream"
            ) as response:
                response.raise_for_status()
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise RuntimeError("Dashboard response is not an SSE stream")
                connected[index].set()
                event = ""
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        field, _, value = line.partition(":")
                        if field == "event":
                            event = value.lstrip(" ")
                        elif field == "data":
                            data.append(value[1:] if value.startswith(" ") else value)
                        continue
                    received_at = time.perf_counter_ns()
                    if event == "task_event" and data:
                        message = json.loads("\n".join(data))
                        if not isinstance(message, dict) or not isinstance(
                            message.get("data"), dict
                        ):
                            raise RuntimeError("Invalid task_event data object")
                        payload = message["data"]
                        if payload.get("benchmark_id") == run_id:
                            sequence = payload.get("sequence")
                            if type(sequence) is not int or not -1 <= sequence < args.events:
                                raise RuntimeError("Invalid benchmark sequence")
                            if sequence == -1:
                                ready[index].set()
                            elif sequence in sent:
                                if sequence in samples[index]:
                                    duplicates[index] += 1
                                else:
                                    samples[index][sequence] = (received_at - sent[sequence]) / 1e6
                                if len(samples[index]) == args.events:
                                    complete[index].set()
                    event, data = "", []
                raise RuntimeError("SSE stream ended before measurement cleanup")
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            errors.append(f"client {index}: {exc}")
            connected[index].set()
            complete[index].set()

    publisher = await asyncio.wait_for(
        nats.connect(args.nats_url, connect_timeout=args.timeout, max_reconnect_attempts=0),
        args.timeout,
    )
    tasks: list[asyncio.Task] = []
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(args.timeout, read=None), trust_env=False
        ) as client:
            tasks = [asyncio.create_task(consume(i, client)) for i in range(args.clients)]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(item.wait() for item in connected)), args.timeout
                )

                async def publish(sequence: int) -> None:
                    if sequence >= 0:
                        sent[sequence] = time.perf_counter_ns()
                    await publisher.publish(
                        "uc.task.event",
                        json.dumps(
                            {
                                "type": "sse_latency_probe",
                                "task_id": f"sse-benchmark-{run_id}",
                                "data": {"benchmark_id": run_id, "sequence": sequence},
                            }
                        ).encode(),
                    )

                deadline = time.monotonic() + args.timeout
                while not all(item.is_set() for item in ready):
                    if errors:
                        raise RuntimeError("; ".join(errors))
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Warmup did not reach every SSE client through NATS")
                    await publish(-1)
                    await publisher.flush(timeout=args.timeout)
                    await asyncio.sleep(0.02)
                for sequence in range(args.events):
                    await publish(sequence)
                    if args.interval_ms:
                        await asyncio.sleep(args.interval_ms / 1000)
                await publisher.flush(timeout=args.timeout)
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(item.wait() for item in complete)), args.timeout
                    )
                except asyncio.TimeoutError:
                    errors.append("Timed out waiting for all published events")
                # Observe delayed duplicates after the last expected frame.
                await asyncio.sleep(0.25)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), args.timeout)
    finally:
        await asyncio.wait_for(publisher.close(), args.timeout)

    clients = [
        summarize(samples[i], args.events, duplicates[i], args.max_latency_ms)
        for i in range(args.clients)
    ]
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "boundary": "NATS publish call to complete HTTP SSE frame; excludes browser rendering",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "events_per_client": args.events,
        "interval_ms": args.interval_ms,
        "limit_ms": args.max_latency_ms,
        "errors": errors,
        "clients": clients,
        "passed": not errors and all(item["passed"] for item in clients),
    }


def finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--events", type=int, default=300)
    parser.add_argument("--clients", type=int, default=3)
    parser.add_argument("--interval-ms", type=finite_number, default=20.0)
    parser.add_argument("--max-latency-ms", type=finite_number, default=200.0)
    parser.add_argument("--timeout", type=finite_number, default=10.0)
    parser.add_argument("--token-env", default="DASHBOARD_PASSWORD")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if min(args.events, args.clients, args.timeout, args.max_latency_ms) <= 0:
        parser.error("events, clients, timeout and max-latency-ms must be positive")
    try:
        evidence = asyncio.run(measure(args))
    except (httpx.HTTPError, nats.errors.Error, OSError, RuntimeError, asyncio.TimeoutError) as exc:
        detail = str(exc) or f"operation exceeded {args.timeout}s deadline"
        print(f"UNAVAILABLE: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 3
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    summary = {key: value for key, value in evidence.items() if key != "clients"}
    summary["clients"] = [
        {key: value for key, value in item.items() if key != "latencies_ms"}
        for item in evidence["clients"]
    ]
    print(json.dumps(summary, indent=2))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
