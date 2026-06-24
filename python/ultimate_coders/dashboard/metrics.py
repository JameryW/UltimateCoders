"""In-memory metrics aggregator for dashboard observability.

Collects task, worker, event, and system metrics via sliding-window
aggregation.  Snapshots are pushed through the SSE/gRPC dashboard stream.

Ponytail: threading.Lock for all state — global lock is fine at dashboard
update rates (~10-50 event/s); per-metric locks if throughput matters.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


# ── Constants ──────────────────────────────────────────────

WINDOW_SECONDS = 3600  # 1h sliding window for event data
TREND_INTERVAL = 60  # sample every 60s
TREND_MAX_SAMPLES = 60  # keep last 60 samples
SLOW_TASK_THRESHOLD_MS = 300_000  # 5 min
ERROR_SPIKE_THRESHOLD = 0.30  # 30% error rate in window
EVENT_RATE_WINDOW = 300  # 5min window for events/min


# ── Data classes ───────────────────────────────────────────

@dataclass
class _TimedEvent:
    """An event timestamped for sliding-window expiry."""
    ts: float  # monotonic seconds
    event_type: str
    duration_ms: float | None = None
    worker_id: str | None = None


@dataclass
class MetricsSample:
    """A single 1-min trend sample point."""
    timestamp: int  # unix seconds
    events_per_minute: float = 0.0
    avg_duration_ms: float = 0.0
    error_rate: float = 0.0
    cluster_utilization: float = 0.0


@dataclass
class TaskMetrics:
    avg_duration_ms: float = 0.0
    p50_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    p99_duration_ms: float = 0.0
    retry_rate: float = 0.0
    slow_tasks_count: int = 0
    total_completed: int = 0
    total_failed: int = 0
    success_rate: float = 0.0


@dataclass
class WorkerMetrics:
    avg_heartbeat_age_seconds: float = 0.0
    per_worker_tool_calls: dict[str, int] = field(default_factory=dict)
    per_worker_subtask_count: dict[str, int] = field(default_factory=dict)
    cluster_load_pct: float = 0.0


@dataclass
class EventMetrics:
    events_per_minute: float = 0.0
    error_spike: bool = False
    event_type_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class SystemMetrics:
    uptime_seconds: int = 0
    circuit_breaker_state: str = "unknown"
    rate_limiter_remaining_ratio: float = 1.0
    cluster_utilization_pct: float = 0.0


@dataclass
class MetricsSnapshot:
    task: TaskMetrics = field(default_factory=TaskMetrics)
    worker: WorkerMetrics = field(default_factory=WorkerMetrics)
    event: EventMetrics = field(default_factory=EventMetrics)
    system: SystemMetrics = field(default_factory=SystemMetrics)
    trend: list[MetricsSample] = field(default_factory=list)


# ── Aggregator ─────────────────────────────────────────────

class MetricsAggregator:
    """Sliding-window metrics aggregator.

    Call ``record_event()`` for every dashboard event; call ``snapshot()``
    to get the current aggregated metrics for SSE/gRPC push.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[_TimedEvent] = deque()
        self._start_time = time.monotonic()
        self._trend: deque[MetricsSample] = deque(maxlen=TREND_MAX_SAMPLES)
        self._last_trend_ts: float = 0.0
        # Cumulative counters (never expire)
        self._total_completed = 0
        self._total_failed = 0
        self._total_retries = 0
        # System state (updated externally)
        self._circuit_breaker_state: str = "unknown"
        self._rate_limiter_remaining: float = 1.0
        self._cluster_utilization_pct: float = 0.0
        self._avg_heartbeat_age: float = 0.0

    # ── Public API ─────────────────────────────────────────

    def record_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Record a dashboard event for metrics aggregation."""
        data = data or {}
        now = time.monotonic()

        duration_ms: float | None = None
        if "duration_ms" in data:
            try:
                duration_ms = float(data["duration_ms"])
            except (ValueError, TypeError):
                pass

        worker_id: str | None = data.get("worker_id") or data.get("assigned_worker")

        with self._lock:
            self._events.append(_TimedEvent(
                ts=now,
                event_type=event_type,
                duration_ms=duration_ms,
                worker_id=str(worker_id) if worker_id else None,
            ))

            # Update cumulative counters
            if event_type in ("task_completed", "subtask_completed"):
                self._total_completed += 1
            elif event_type in ("task_failed", "subtask_failed"):
                self._total_failed += 1
            elif event_type == "subtask_retrying":
                self._total_retries += 1

    def update_system_state(
        self,
        *,
        circuit_breaker_state: str | None = None,
        rate_limiter_remaining: float | None = None,
        cluster_utilization_pct: float | None = None,
        avg_heartbeat_age: float | None = None,
    ) -> None:
        """Update system-level state from external sources (health, workers)."""
        with self._lock:
            if circuit_breaker_state is not None:
                self._circuit_breaker_state = circuit_breaker_state
            if rate_limiter_remaining is not None:
                self._rate_limiter_remaining = rate_limiter_remaining
            if cluster_utilization_pct is not None:
                self._cluster_utilization_pct = cluster_utilization_pct
            if avg_heartbeat_age is not None:
                self._avg_heartbeat_age = avg_heartbeat_age

    def snapshot(self) -> MetricsSnapshot:
        """Return the current aggregated metrics snapshot."""
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        event_rate_cutoff = now - EVENT_RATE_WINDOW

        with self._lock:
            # Purge expired events
            while self._events and self._events[0].ts < cutoff:
                self._events.popleft()

            # Collect active events
            recent = list(self._events)

            # ── Task metrics ───────────────────────────────
            completed_events = [
                e for e in recent
                if e.event_type in ("task_completed", "subtask_completed")
                and e.duration_ms is not None
            ]
            durations = sorted(e.duration_ms for e in completed_events if e.duration_ms is not None and e.duration_ms > 0)

            if durations:
                avg = sum(durations) / len(durations)
                p50 = _percentile(durations, 50)
                p95 = _percentile(durations, 95)
                p99 = _percentile(durations, 99)
            else:
                avg = p50 = p95 = p99 = 0.0

            slow_count = sum(1 for d in durations if d >= SLOW_TASK_THRESHOLD_MS)
            total_outcomes = self._total_completed + self._total_failed
            retry_rate = self._total_retries / total_outcomes if total_outcomes > 0 else 0.0
            success_rate = self._total_completed / total_outcomes if total_outcomes > 0 else 0.0

            task = TaskMetrics(
                avg_duration_ms=avg,
                p50_duration_ms=p50,
                p95_duration_ms=p95,
                p99_duration_ms=p99,
                retry_rate=retry_rate,
                slow_tasks_count=slow_count,
                total_completed=self._total_completed,
                total_failed=self._total_failed,
                success_rate=success_rate,
            )

            # ── Worker metrics ─────────────────────────────
            tool_calls: dict[str, int] = {}
            subtask_counts: dict[str, int] = {}
            for e in recent:
                if e.worker_id is None:
                    continue
                if e.event_type in ("tool_call", "tool_invoked"):
                    tool_calls[e.worker_id] = tool_calls.get(e.worker_id, 0) + 1
                if e.event_type in ("subtask_completed", "subtask_failed"):
                    subtask_counts[e.worker_id] = subtask_counts.get(e.worker_id, 0) + 1

            worker = WorkerMetrics(
                avg_heartbeat_age_seconds=self._avg_heartbeat_age,
                per_worker_tool_calls=tool_calls,
                per_worker_subtask_count=subtask_counts,
                cluster_load_pct=self._cluster_utilization_pct,
            )

            # ── Event metrics ──────────────────────────────
            rate_events = [e for e in recent if e.ts >= event_rate_cutoff]
            rate_window_seconds = min(EVENT_RATE_WINDOW, now - rate_events[0].ts) if rate_events else EVENT_RATE_WINDOW
            events_per_minute = len(rate_events) / (rate_window_seconds / 60) if rate_window_seconds > 0 else 0.0

            # Error spike: error rate in event rate window
            rate_completed = sum(1 for e in rate_events if e.event_type in ("task_completed", "subtask_completed"))
            rate_failed = sum(1 for e in rate_events if e.event_type in ("task_failed", "subtask_failed"))
            rate_total = rate_completed + rate_failed
            error_rate = rate_failed / rate_total if rate_total > 0 else 0.0
            error_spike = error_rate > ERROR_SPIKE_THRESHOLD and rate_total >= 3

            type_counts: dict[str, int] = {}
            for e in recent:
                type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1

            event = EventMetrics(
                events_per_minute=events_per_minute,
                error_spike=error_spike,
                event_type_counts=type_counts,
            )

            # ── System metrics ─────────────────────────────
            system = SystemMetrics(
                uptime_seconds=int(now - self._start_time),
                circuit_breaker_state=self._circuit_breaker_state,
                rate_limiter_remaining_ratio=self._rate_limiter_remaining,
                cluster_utilization_pct=self._cluster_utilization_pct,
            )

            # ── Trend sampling ─────────────────────────────
            if now - self._last_trend_ts >= TREND_INTERVAL:
                self._last_trend_ts = now
                self._trend.append(MetricsSample(
                    timestamp=int(time.time()),
                    events_per_minute=events_per_minute,
                    avg_duration_ms=avg,
                    error_rate=error_rate,
                    cluster_utilization=self._cluster_utilization_pct,
                ))

            trend = list(self._trend)

        return MetricsSnapshot(task=task, worker=worker, event=event, system=system, trend=trend)


# ── Helpers ────────────────────────────────────────────────

def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute percentile from a sorted list. Returns 0.0 for empty list."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    k = (pct / 100.0) * (n - 1)
    f = math.floor(k)
    c = min(math.ceil(k), n - 1)
    if f == c:
        return sorted_values[f]
    # Linear interpolation
    return sorted_values[f] + (k - f) * (sorted_values[c] - sorted_values[f])
