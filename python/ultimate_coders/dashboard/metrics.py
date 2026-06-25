"""In-memory metrics aggregator for dashboard observability.

Collects task, worker, event, and system metrics via sliding-window
aggregation.  Snapshots are pushed through the SSE/gRPC dashboard stream.

Ponytail: threading.Lock for all state — global lock is fine at dashboard
update rates (~10-50 event/s); per-metric locks if throughput matters.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ── Prometheus export (optional dependency) ──────────────
try:
    import prometheus_client as pc  # type: ignore[import-untyped]
    _PROM_AVAILABLE = True
except ImportError:
    pc = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False

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
    recent_failed: int = 0  # failures within the sliding window
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


# ── Alert system ───────────────────────────────────────────

@dataclass
class AlertConfig:
    """Configurable thresholds for alert conditions."""
    stale_worker_threshold_seconds: float = 120.0
    circuit_breaker_alert: bool = True
    rate_limiter_threshold_pct: float = 80.0
    failure_window_minutes: float = 60.0
    failure_count_threshold: int = 5
    high_latency_ms: float = 300_000.0  # 5 min
    error_spike_alert: bool = True
    slow_tasks_alert: bool = True
    high_latency_alert: bool = True


@dataclass
class Alert:
    """A single alert event."""
    alert_type: str
    message: str
    severity: str  # "warning" | "critical"
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False


# ── Aggregator ─────────────────────────────────────────────

class MetricsAggregator:
    """Sliding-window metrics aggregator.

    Call ``record_event()`` for every dashboard event; call ``snapshot()``
    to get the current aggregated metrics for SSE/gRPC push.
    """

    def __init__(self, alert_config: AlertConfig | None = None) -> None:
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
        # Alert system
        self._alert_config = alert_config or AlertConfig()
        self._alert_store = AlertStore()
        self._active_alert_types: set[str] = set()  # track which types are currently firing
        # Prometheus exporter (no-op if prometheus_client not installed)
        self._prom = PrometheusExporter()
        # Time-series store (SQLite)
        self._metrics_store = MetricsStore()
        # Restore trend from SQLite on startup
        # TREND_MAX_SAMPLES samples at TREND_INTERVAL seconds each
        restore_minutes = TREND_MAX_SAMPLES * TREND_INTERVAL // 60
        self._trend = deque(
            self._metrics_store.get_trend(minutes=restore_minutes),
            maxlen=TREND_MAX_SAMPLES,
        )

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

        # Sync to Prometheus (outside lock — prometheus_client has its own)
        self._prom.record_event(event_type, data)

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

        # Sync to Prometheus (outside lock)
        self._prom.update_system_state(
            circuit_breaker_state=circuit_breaker_state,
            rate_limiter_remaining=rate_limiter_remaining,
            cluster_utilization_pct=cluster_utilization_pct,
        )

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
            durations = sorted(
                e.duration_ms for e in completed_events
                if e.duration_ms is not None and e.duration_ms > 0
            )

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
            # Count failures within the sliding window (for alerting)
            _fail_types = ("task_failed", "subtask_failed")
            recent_failed = sum(1 for e in recent if e.event_type in _fail_types)

            task = TaskMetrics(
                avg_duration_ms=avg,
                p50_duration_ms=p50,
                p95_duration_ms=p95,
                p99_duration_ms=p99,
                retry_rate=retry_rate,
                slow_tasks_count=slow_count,
                total_completed=self._total_completed,
                total_failed=self._total_failed,
                recent_failed=recent_failed,
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
            rate_window_seconds = (
                min(EVENT_RATE_WINDOW, now - rate_events[0].ts)
                if rate_events else EVENT_RATE_WINDOW
            )
            events_per_minute = (
                len(rate_events) / (rate_window_seconds / 60)
                if rate_window_seconds > 0 else 0.0
            )

            # Error spike: error rate in event rate window
            _completed = ("task_completed", "subtask_completed")
            _failed = ("task_failed", "subtask_failed")
            rate_completed = sum(1 for e in rate_events if e.event_type in _completed)
            rate_failed = sum(1 for e in rate_events if e.event_type in _failed)
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
                sample = MetricsSample(
                    timestamp=int(time.time()),
                    events_per_minute=events_per_minute,
                    avg_duration_ms=avg,
                    error_rate=error_rate,
                    cluster_utilization=self._cluster_utilization_pct,
                )
                self._trend.append(sample)
                # Persist to SQLite
                self._metrics_store.insert(sample)

            trend = list(self._trend)

        return MetricsSnapshot(task=task, worker=worker, event=event, system=system, trend=trend)

    # ── Alert checking ─────────────────────────────────────

    def check_alerts(self, snap: MetricsSnapshot) -> tuple[list[Alert], list[str]]:
        """Check alert conditions against a snapshot.

        Returns (newly_triggered_alerts, resolved_alert_types).
        Compares current state against configurable thresholds. New alerts are
        persisted to SQLite. Resolved alerts are marked in the store.
        """
        cfg = self._alert_config
        now = time.time()
        new_alerts: list[Alert] = []
        current_types: set[str] = set()

        # 1. Error spike
        if cfg.error_spike_alert and snap.event.error_spike:
            current_types.add("error_spike")
            if "error_spike" not in self._active_alert_types:
                a = Alert("error_spike", "Error rate spike detected (>30%)", "critical", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 2. Slow tasks
        if cfg.slow_tasks_alert and snap.task.slow_tasks_count > 0:
            current_types.add("slow_tasks")
            if "slow_tasks" not in self._active_alert_types:
                msg = f"{snap.task.slow_tasks_count} slow task(s) (>5min)"
                a = Alert("slow_tasks", msg, "warning", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 3. High latency
        if cfg.high_latency_alert and snap.task.p95_duration_ms > cfg.high_latency_ms:
            current_types.add("high_latency")
            if "high_latency" not in self._active_alert_types:
                p95 = snap.task.p95_duration_ms
                threshold = cfg.high_latency_ms
                msg = f"P95 latency {p95:.0f}ms exceeds {threshold:.0f}ms"
                a = Alert("high_latency", msg, "warning", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 4. Circuit breaker open
        if cfg.circuit_breaker_alert and snap.system.circuit_breaker_state == "open":
            current_types.add("circuit_breaker_open")
            if "circuit_breaker_open" not in self._active_alert_types:
                a = Alert("circuit_breaker_open", "Circuit breaker OPEN", "critical", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 5. Rate limiter high
        used_pct = (1 - snap.system.rate_limiter_remaining_ratio) * 100
        if used_pct >= cfg.rate_limiter_threshold_pct:
            current_types.add("rate_limiter_high")
            if "rate_limiter_high" not in self._active_alert_types:
                a = Alert("rate_limiter_high", f"Rate limiter {used_pct:.0f}% used", "warning", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 6. Stale workers (using heartbeat age)
        if snap.worker.avg_heartbeat_age_seconds > cfg.stale_worker_threshold_seconds:
            current_types.add("stale_workers")
            if "stale_workers" not in self._active_alert_types:
                age = snap.worker.avg_heartbeat_age_seconds
                threshold = cfg.stale_worker_threshold_seconds
                msg = f"Avg heartbeat age {age:.1f}s (>{threshold:.0f}s)"
                a = Alert("stale_workers", msg, "warning", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # 7. Recent failures (within sliding window)
        if snap.task.recent_failed > 0:
            current_types.add("recent_failures")
            if "recent_failures" not in self._active_alert_types \
                    and snap.task.recent_failed >= cfg.failure_count_threshold:
                msg = f"{snap.task.recent_failed} recent failure(s) (1h window)"
                a = Alert("recent_failures", msg, "critical", now)
                self._alert_store.insert(a)
                new_alerts.append(a)

        # Resolve alerts that are no longer active
        resolved_types = self._active_alert_types - current_types
        for t in resolved_types:
            self._alert_store.resolve(t)

        self._active_alert_types = current_types
        return new_alerts, list(resolved_types)

    @property
    def alert_store(self) -> AlertStore:
        return self._alert_store

    def generate_prometheus(self) -> str:
        """Generate Prometheus text format output for /metrics endpoint."""
        return self._prom.generate()

    def get_trend(self, minutes: int = 60) -> list[MetricsSample]:
        """Get trend samples for the last N minutes from SQLite."""
        return self._metrics_store.get_trend(minutes)


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


# ── AlertStore (SQLite persistence) ──────────────────────────

_METRICS_DB_DEFAULT = os.path.expanduser("~/.ultimate_coders/metrics.db")
_ALERTS_DB_PATH = os.environ.get("UC_METRICS_DB", _METRICS_DB_DEFAULT)


class AlertStore:
    """SQLite-backed alert history store.

    ponytail: single WAL-mode db file, shared with MetricsStore (PR3).
    No separate db for alerts — same file, different table.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _ALERTS_DB_PATH
        self._local = threading.local()  # per-thread connections
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                resolved INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

    def insert(self, alert: Alert) -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO alerts "
            "(timestamp, alert_type, message, severity, resolved) "
            "VALUES (?, ?, ?, ?, ?)",
            (alert.timestamp, alert.alert_type, alert.message, alert.severity, int(alert.resolved)),
        )
        conn.commit()
        return cur.lastrowid or 0

    def resolve(self, alert_type: str) -> None:
        """Mark all active alerts of this type as resolved."""
        conn = self._conn()
        conn.execute(
            "UPDATE alerts SET resolved = 1 WHERE alert_type = ? AND resolved = 0",
            (alert_type,),
        )
        conn.commit()

    def get_recent(self, limit: int = 100) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, timestamp, alert_type, message, severity, resolved "
            "FROM alerts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, timestamp, alert_type, message, severity "
            "FROM alerts WHERE resolved = 0 ORDER BY id DESC",
        ).fetchall()
        return [dict(r) for r in rows]


# ── MetricsStore (SQLite time-series persistence) ──────────

_METRICS_RETENTION_DAYS = int(os.environ.get("UC_METRICS_RETENTION_DAYS", "7"))


class MetricsStore:
    """SQLite-backed time-series store for trend samples.

    Same db file as AlertStore — different table.  Enables >1h trend
    lookback and restart recovery.

    ponytail: no PostgreSQL fallback, SQLite is everywhere, data is tiny.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _ALERTS_DB_PATH
        self._local = threading.local()
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics_samples (
                timestamp INTEGER NOT NULL PRIMARY KEY,
                events_per_minute REAL NOT NULL DEFAULT 0,
                avg_duration_ms REAL NOT NULL DEFAULT 0,
                error_rate REAL NOT NULL DEFAULT 0,
                cluster_utilization REAL NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

    def insert(self, sample: MetricsSample) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO metrics_samples "
            "(timestamp, events_per_minute, avg_duration_ms, "
            "error_rate, cluster_utilization) VALUES (?, ?, ?, ?, ?)",
            (sample.timestamp, sample.events_per_minute,
             sample.avg_duration_ms, sample.error_rate,
             sample.cluster_utilization),
        )
        conn.commit()
        # Retention cleanup — approximately once per hour (when timestamp is near the hour mark)
        if sample.timestamp % 3600 < 60:
            cutoff = sample.timestamp - _METRICS_RETENTION_DAYS * 86400
            conn.execute("DELETE FROM metrics_samples WHERE timestamp < ?", (cutoff,))
            conn.commit()

    def get_trend(self, minutes: int = 60) -> list[MetricsSample]:
        """Get trend samples for the last N minutes."""
        cutoff = int(time.time()) - minutes * 60
        conn = self._conn()
        rows = conn.execute(
            "SELECT timestamp, events_per_minute, avg_duration_ms, "
            "error_rate, cluster_utilization "
            "FROM metrics_samples WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()
        return [
            MetricsSample(
                timestamp=r["timestamp"],
                events_per_minute=r["events_per_minute"],
                avg_duration_ms=r["avg_duration_ms"],
                error_rate=r["error_rate"],
                cluster_utilization=r["cluster_utilization"],
            )
            for r in rows
        ]


# ── Prometheus Exporter ─────────────────────────────────────

class PrometheusExporter:
    """Export metrics in Prometheus text format.

    Keeps prometheus_client gauges/counters/histograms in sync with
    MetricsAggregator data.  Complementary: MetricsAggregator gives
    pre-computed aggregates for the browser; prometheus_client gives
    raw counters for Prometheus server (where PromQL computes rates).

    ponytail: no make_asgi_app mount, no start_http_server — just a
    FastAPI route calling generate_latest().
    """

    def __init__(self) -> None:
        if not _PROM_AVAILABLE:
            self._enabled = False
            return
        self._enabled = True
        registry = pc.CollectorRegistry()

        self.tasks_completed = pc.Counter(
            "uc_tasks_completed_total", "Total completed tasks", registry=registry,
        )
        self.tasks_failed = pc.Counter(
            "uc_tasks_failed_total", "Total failed tasks", registry=registry,
        )
        self.task_duration = pc.Histogram(
            "uc_task_duration_seconds", "Task duration in seconds",
            buckets=[10, 30, 60, 300, 600, 1800], registry=registry,
        )
        self.subtask_retries = pc.Counter(
            "uc_subtask_retries_total", "Total subtask retries", registry=registry,
        )
        self.events = pc.Counter(
            "uc_events_total", "Total events", ["event_type"], registry=registry,
        )
        self.workers_heartbeat_age = pc.Gauge(
            "uc_workers_heartbeat_age_seconds",
            "Worker heartbeat age", ["worker_id"],
            registry=registry,
        )
        self.cluster_utilization = pc.Gauge(
            "uc_cluster_utilization",
            "Cluster utilization ratio", registry=registry,
        )
        self.circuit_breaker_state = pc.Gauge(
            "uc_circuit_breaker_state",
            "Circuit breaker state (0=closed, 0.5=half_open, 1=open)",
            registry=registry,
        )
        self.rate_limiter_remaining = pc.Gauge(
            "uc_rate_limiter_remaining_ratio", "Rate limiter remaining ratio", registry=registry,
        )
        self._registry = registry

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        if not self._enabled:
            return
        data = data or {}

        if event_type in ("task_completed", "subtask_completed"):
            self.tasks_completed.inc()
            if "duration_ms" in data:
                try:
                    self.task_duration.observe(float(data["duration_ms"]) / 1000)
                except (ValueError, TypeError):
                    pass
        elif event_type in ("task_failed", "subtask_failed"):
            self.tasks_failed.inc()
        elif event_type == "subtask_retrying":
            self.subtask_retries.inc()

        self.events.labels(event_type=event_type).inc()

        # Worker heartbeat age — set per-worker gauge
        worker_id = data.get("worker_id") or data.get("assigned_worker")
        if worker_id and "heartbeat_age" in data:
            try:
                self.workers_heartbeat_age.labels(
                    worker_id=str(worker_id)
                ).set(float(data["heartbeat_age"]))
            except (ValueError, TypeError):
                pass

    def update_system_state(
        self,
        *,
        circuit_breaker_state: str | None = None,
        rate_limiter_remaining: float | None = None,
        cluster_utilization_pct: float | None = None,
    ) -> None:
        if not self._enabled:
            return
        if circuit_breaker_state is not None:
            state_map = {"closed": 0, "half_open": 0.5, "open": 1}
            self.circuit_breaker_state.set(state_map.get(circuit_breaker_state, -1))
        if rate_limiter_remaining is not None:
            self.rate_limiter_remaining.set(rate_limiter_remaining)
        if cluster_utilization_pct is not None:
            self.cluster_utilization.set(cluster_utilization_pct / 100)

    def generate(self) -> str:
        """Generate Prometheus text format output."""
        if not self._enabled:
            return "# prometheus_client not installed\n"
        return pc.generate_latest(self._registry).decode("utf-8")
