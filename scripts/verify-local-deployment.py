"""Probe a running Dashboard and optionally execute one explicitly supplied task.

Only --task submits work. Use an isolated repository for coding verification.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request(base: str, path: str, body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        payload = response.read()
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            value = payload.decode(errors="replace")
        return response.status, value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8081")
    parser.add_argument("--task", help="Explicitly submit this coding task")
    parser.add_argument("--project", default="")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    if args.task and not args.project.strip():
        parser.error("--task requires a nonempty --project execution scope")
    for path in ["/", "/dashboard", "/dashboard/api/health", "/dashboard/api/workers",
                 "/dashboard/api/tasks", "/dashboard/api/scheduler", "/dashboard/api/events",
                 "/dashboard/api/repos", "/dashboard/api/alerts", "/dashboard/api/trend"]:
        status, value = request(args.url, path)
        if status != 200:
            raise RuntimeError(f"GET {path}: {status} {value}")
        if path.endswith("/health") and (
            not isinstance(value, dict) or value.get("available") is not True
            or value.get("status") != "healthy"
        ):
            raise RuntimeError(f"Gateway is unavailable or unhealthy: {value}")
        if path.endswith("/workers") and (
            not isinstance(value, dict) or value.get("available") is not True
            or value.get("available_count", 0) < 1
        ):
            raise RuntimeError(f"No available execution worker: {value}")
        if path.endswith("/tasks") and (
            not isinstance(value, dict) or value.get("available") is not True
        ):
            raise RuntimeError(f"Task service is unavailable: {value}")
        print(f"PASS GET {path}", flush=True)
    for body in [[], {"description": 7}, {"description": "work", "project_id": []}]:
        status, value = request(args.url, "/dashboard/api/tasks/submit", body)
        if status != 400 or not isinstance(value, dict) or value.get("success") is not False:
            raise RuntimeError(f"Invalid task input accepted: {status} {value}")
    print("PASS invalid task inputs rejected", flush=True)
    if not args.task:
        return
    status, value = request(args.url, "/dashboard/api/tasks/submit", {
        "description": args.task, "project_id": args.project,
        "agent_config": {"agent": "local-harness", "max_turns": 12},
    })
    if status != 200 or not isinstance(value, dict) or not value.get("success"):
        raise RuntimeError(f"Submission failed: {status} {value}")
    task_id = value["task_id"]
    print(f"Submitted task {task_id}", flush=True)
    deadline = time.monotonic() + args.timeout
    previous = None
    while time.monotonic() < deadline:
        status, snapshot = request(args.url, "/dashboard/api/tasks")
        if status != 200 or not isinstance(snapshot, dict):
            raise RuntimeError(f"Task snapshot failed: {status} {snapshot}")
        task = next((t for t in snapshot.get("tasks", []) if t.get("id") == task_id), None)
        state = str(task.get("status", "")).lower() if task else "awaiting snapshot"
        if state != previous:
            print(f"Task {task_id}: {state}", flush=True)
            previous = state
        if state in {"completed", "complete", "succeeded"}:
            print(json.dumps(task, ensure_ascii=False), flush=True)
            return
        if state in {"failed", "cancelled", "canceled"}:
            raise RuntimeError(f"Task did not succeed: {task}")
        time.sleep(1)
    raise TimeoutError(f"Task {task_id} did not complete in {args.timeout}s; state={previous}")


if __name__ == "__main__":
    main()
