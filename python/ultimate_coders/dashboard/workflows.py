"""Turn the flat dashboard event stream into replayable workflow runs.

A workflow step emits ``subtask_progress`` events whose ``data`` carries
``step_index`` / ``step_total`` / ``step_agent`` / ``step_status`` (and often
``step_summary``). The durable NATS history and the in-memory fallback both
deliver those events as separate records, one per transition. Replaying a run
therefore means grouping the transitions back into runs and steps -- a job the
product home page should not have to redo in the browser, and one that is worth
testing without HTTP.

The aggregation is deliberately tolerant and bounded:

* only ``subtask_progress`` events with a usable step index and status count;
* malformed events are skipped, never fatal;
* runs are returned newest-first and capped (default 20);
* steps per run are capped (default 50) so a busy cluster cannot make one
  replay response unbounded.

Everything here is read-only; no event is mutated or re-published.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

MAX_REPLAY_RUNS = 20
MAX_REPLAY_STEPS = 50

# Statuses written by ``Worker._emit_step_event`` in
# ``python/ultimate_coders/agent/worker.py``.
STEP_STARTED = "started"
STEP_RETRYING = "retrying"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

_STEP_STATUSES = frozenset(
    {STEP_STARTED, STEP_RETRYING, STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED}
)
_TERMINAL_STATUSES = frozenset({STEP_COMPLETED, STEP_FAILED, STEP_SKIPPED})
_FRACTIONAL_RE = re.compile(r"(\.\d+)(?=[+-]\d\d:?\d\d$|$)")


def _timestamp(value: Any) -> datetime | None:
    """Parse an event timestamp, normalising naive values to UTC (Py 3.9+ safe)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python 3.9/3.10 fromisoformat only accepts 3 or 6 fractional digits.
    match = _FRACTIONAL_RE.search(text)
    if match:
        frac = match.group(1)[1:]
        norm = (frac + "000000")[:6]
        text = text[: match.start(1)] + "." + norm + text[match.end(1) :]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any, *, limit: int = 200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return the event's detail mapping.

    The REST API returns ``data``; a few older in-process producers used
    ``details``. Accept both so the replay does not silently go empty when the
    event *source* changes shape.
    """
    for key in ("data", "details"):
        value = event.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _as_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _duration_ms(started: datetime | None, finished: datetime | None) -> int | None:
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _run_status(steps: list[dict[str, Any]]) -> str:
    statuses = [step["status"] for step in steps]
    if STEP_FAILED in statuses:
        return "failed"
    if any(status in (STEP_STARTED, STEP_RETRYING) for status in statuses):
        return "running"
    if statuses and all(status in _TERMINAL_STATUSES for status in statuses):
        return "completed"
    return "unknown"


def build_workflow_replays(
    events: Sequence[Mapping[str, Any]],
    *,
    max_runs: int = MAX_REPLAY_RUNS,
    max_steps: int = MAX_REPLAY_STEPS,
) -> list[dict[str, Any]]:
    """Group step-progress events into bounded, newest-first replay runs.

    Args:
        events: Flat event dictionaries, in any order. Timestamps decide the
            final order; the input order only breaks ties.
        max_runs: Maximum runs to return (clamped to ``MAX_REPLAY_RUNS``).
        max_steps: Maximum steps per run (clamped to ``MAX_REPLAY_STEPS``).

    Returns:
        Newest-first run summaries. Each run carries the task/subtask identity,
        an overall status, timing and an ordered ``steps`` list. Steps include
        the agent, latest status/summary, retry attempt and wall-clock
        duration when both endpoints are visible.
    """
    effective_runs = max(0, min(int(max_runs), MAX_REPLAY_RUNS))
    effective_steps = max(0, min(int(max_steps), MAX_REPLAY_STEPS))
    if effective_runs == 0 or effective_steps == 0:
        return []

    records: list[dict[str, Any]] = []
    for position, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        if _as_text(event.get("type"), limit=64).lower() != "subtask_progress":
            continue
        payload = _payload(event)
        step_index = _as_int(payload.get("step_index", event.get("step_index")))
        if step_index is None or step_index < 0:
            continue
        status = _as_text(
            payload.get("step_status", event.get("step_status")), limit=32
        ).lower()
        if status not in _STEP_STATUSES:
            continue
        task_id = _as_text(
            event.get("task_id") or payload.get("task_id"), limit=160
        )
        subtask_id = _as_text(
            event.get("subtask_id")
            or payload.get("subtask_id")
            or payload.get("node_id"),
            limit=160,
        )
        if not task_id and not subtask_id:
            continue
        step_total = _as_int(payload.get("step_total", event.get("step_total")))
        timestamp = _timestamp(event.get("timestamp"))
        percent = _as_int(payload.get("percent", event.get("percent")))
        records.append(
            {
                "position": position,
                "task_id": task_id,
                "subtask_id": subtask_id,
                "step_index": step_index,
                "step_total": step_total,
                "status": status,
                "agent": _as_text(
                    payload.get("step_agent", event.get("step_agent")), limit=120
                ),
                "summary": _as_text(
                    payload.get("step_summary", event.get("step_summary"))
                ),
                "retry_attempt": _as_int(
                    payload.get("retry_attempt", event.get("retry_attempt"))
                ),
                "parallel_group": _as_text(
                    payload.get("parallel_group", event.get("parallel_group")),
                    limit=120,
                ),
                "percent": percent if percent is not None and 0 <= percent <= 100 else None,
                "timestamp": timestamp,
            }
        )

    # Oldest first makes "latest transition wins" a plain assignment. Events
    # without a timestamp sort last; their input position still orders them.
    records.sort(
        key=lambda record: (
            record["timestamp"] is None,
            record["timestamp"].timestamp() if record["timestamp"] else 0.0,
            record["position"],
        )
    )

    runs: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["task_id"], record["subtask_id"])
        run = runs.setdefault(
            key,
            {
                "task_id": record["task_id"],
                "subtask_id": record["subtask_id"],
                "started": None,
                "updated": None,
                "step_total": 0,
                "percent": None,
                "steps": {},
            },
        )
        timestamp = record["timestamp"]
        if timestamp is not None:
            if run["started"] is None or timestamp < run["started"]:
                run["started"] = timestamp
            if run["updated"] is None or timestamp > run["updated"]:
                run["updated"] = timestamp
        if record["step_total"] is not None:
            run["step_total"] = max(run["step_total"], record["step_total"])
        if record["percent"] is not None:
            run["percent"] = record["percent"]

        step = run["steps"].setdefault(
            record["step_index"],
            {
                "index": record["step_index"],
                "agent": "",
                "status": record["status"],
                "summary": "",
                "retry_attempt": None,
                "parallel_group": "",
                "started": None,
                "finished": None,
            },
        )
        if record["status"] in (STEP_STARTED, STEP_RETRYING):
            step["finished"] = None
        if timestamp is not None:
            if step["started"] is None or timestamp < step["started"]:
                step["started"] = timestamp
            if record["status"] in _TERMINAL_STATUSES and (
                step["finished"] is None or timestamp > step["finished"]
            ):
                step["finished"] = timestamp
        step["status"] = record["status"]
        if record["agent"]:
            step["agent"] = record["agent"]
        if record["summary"]:
            step["summary"] = record["summary"]
        if record["retry_attempt"] is not None:
            step["retry_attempt"] = record["retry_attempt"]
        if record["parallel_group"]:
            step["parallel_group"] = record["parallel_group"]

    replays: list[tuple[datetime | None, dict[str, Any]]] = []
    for run in runs.values():
        all_indices = sorted(run["steps"])
        if not all_indices:
            continue
        all_raw_steps = [run["steps"][index] for index in all_indices]
        overall_status = _run_status(all_raw_steps)
        completed = sum(
            1 for step in all_raw_steps if step["status"] in (STEP_COMPLETED, STEP_SKIPPED)
        )
        step_indices = all_indices[:effective_steps]
        steps: list[dict[str, Any]] = []
        for index in step_indices:
            raw_step = run["steps"][index]
            steps.append(
                {
                    "index": index,
                    "agent": raw_step["agent"],
                    "status": raw_step["status"],
                    "summary": raw_step["summary"],
                    "retry_attempt": raw_step["retry_attempt"],
                    "parallel_group": raw_step["parallel_group"] or None,
                    "started_at": _as_iso(raw_step["started"]),
                    "finished_at": _as_iso(raw_step["finished"]),
                    "duration_ms": _duration_ms(
                        raw_step["started"], raw_step["finished"]
                    ),
                }
            )
        prefix = run["task_id"] or "task"
        subtask = run["subtask_id"] or "subtask"
        replays.append(
            (
                run["updated"],
                {
                    "id": f"{prefix}:{subtask}",
                    "task_id": run["task_id"],
                    "subtask_id": run["subtask_id"],
                    "status": overall_status,
                    "step_total": max(run["step_total"], len(all_indices)),
                    "step_count": len(steps),
                    "completed_steps": completed,
                    "percent": run["percent"],
                    "started_at": _as_iso(run["started"]),
                    "updated_at": _as_iso(run["updated"]),
                    "steps": steps,
                },
            )
        )

    replays.sort(
        key=lambda item: (
            item[0] is not None,
            item[0].timestamp() if item[0] is not None else float("-inf"),
            item[1]["id"],
        ),
        reverse=True,
    )
    return [replay for _, replay in replays[:effective_runs]]
