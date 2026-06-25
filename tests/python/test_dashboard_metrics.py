"""Tests for the Dashboard metrics module (AlertConfig, AlertStore, MetricsStore,
PrometheusExporter, and check_alerts).

Uses temp SQLite databases so tests never touch the real metrics.db.
"""

from __future__ import annotations

import os
import time

import pytest
from ultimate_coders.dashboard.metrics import (
    Alert,
    AlertConfig,
    AlertStore,
    MetricsAggregator,
    MetricsSample,
    MetricsStore,
    PrometheusExporter,
)

# ── Helpers ──────────────────────────────────────────────────


def _make_alert_store(tmp_path: str) -> AlertStore:
    """Create an AlertStore with a temp database."""
    db_path = os.path.join(tmp_path, "test-metrics.db")
    return AlertStore(db_path=db_path)


def _make_metrics_store(tmp_path: str) -> MetricsStore:
    """Create a MetricsStore with a temp database."""
    db_path = os.path.join(tmp_path, "test-metrics.db")
    return MetricsStore(db_path=db_path)


def _make_aggregator(tmp_path: str, alert_config: AlertConfig | None = None) -> MetricsAggregator:
    """Create a MetricsAggregator with temp SQLite stores."""
    db_path = os.path.join(tmp_path, "test-metrics.db")
    agg = MetricsAggregator.__new__(MetricsAggregator)
    agg._lock = __import__("threading").Lock()
    agg._events = __import__("collections").deque()
    agg._start_time = time.monotonic()
    agg._trend = __import__("collections").deque(maxlen=60)
    agg._last_trend_ts = 0.0
    agg._total_completed = 0
    agg._total_failed = 0
    agg._total_retries = 0
    agg._circuit_breaker_state = "unknown"
    agg._rate_limiter_remaining = 1.0
    agg._cluster_utilization_pct = 0.0
    agg._avg_heartbeat_age = 0.0
    agg._alert_config = alert_config or AlertConfig()
    agg._alert_store = AlertStore(db_path=db_path)
    agg._active_alert_types = set()
    agg._prom = PrometheusExporter()
    agg._metrics_store = MetricsStore(db_path=db_path)
    agg._trend = __import__("collections").deque(
        agg._metrics_store.get_trend(minutes=60), maxlen=60
    )
    return agg


# ── AlertConfig Tests ────────────────────────────────────────


class TestAlertConfig:
    """Test AlertConfig dataclass defaults."""

    def test_defaults(self):
        cfg = AlertConfig()
        assert cfg.stale_worker_threshold_seconds == 120.0
        assert cfg.circuit_breaker_alert is True
        assert cfg.rate_limiter_threshold_pct == 80.0
        assert cfg.failure_window_minutes == 60.0
        assert cfg.failure_count_threshold == 5
        assert cfg.high_latency_ms == 300_000.0
        assert cfg.error_spike_alert is True
        assert cfg.slow_tasks_alert is True
        assert cfg.high_latency_alert is True

    def test_custom_thresholds(self):
        cfg = AlertConfig(
            stale_worker_threshold_seconds=300.0,
            rate_limiter_threshold_pct=90.0,
            failure_count_threshold=10,
        )
        assert cfg.stale_worker_threshold_seconds == 300.0
        assert cfg.rate_limiter_threshold_pct == 90.0
        assert cfg.failure_count_threshold == 10

    def test_disable_alerts(self):
        cfg = AlertConfig(
            error_spike_alert=False,
            slow_tasks_alert=False,
            high_latency_alert=False,
            circuit_breaker_alert=False,
        )
        assert cfg.error_spike_alert is False
        assert cfg.slow_tasks_alert is False
        assert cfg.high_latency_alert is False
        assert cfg.circuit_breaker_alert is False


# ── Alert Tests ──────────────────────────────────────────────


class TestAlert:
    """Test Alert dataclass."""

    def test_alert_defaults(self):
        a = Alert("test_type", "test message", "warning")
        assert a.alert_type == "test_type"
        assert a.message == "test message"
        assert a.severity == "warning"
        assert a.timestamp > 0
        assert a.resolved is False

    def test_alert_critical(self):
        a = Alert("error_spike", "Error spike!", "critical")
        assert a.severity == "critical"


# ── AlertStore Tests ─────────────────────────────────────────


class TestAlertStore:
    """Test AlertStore SQLite CRUD."""

    def test_insert_and_get_recent(self, tmp_path):
        store = _make_alert_store(tmp_path)
        a = Alert("error_spike", "Error spike detected", "critical")
        row_id = store.insert(a)
        assert row_id > 0

        recent = store.get_recent(limit=10)
        assert len(recent) == 1
        assert recent[0]["alert_type"] == "error_spike"
        assert recent[0]["message"] == "Error spike detected"
        assert recent[0]["severity"] == "critical"
        assert recent[0]["resolved"] == 0

    def test_resolve_alert(self, tmp_path):
        store = _make_alert_store(tmp_path)
        store.insert(Alert("stale_workers", "Workers stale", "warning"))
        store.resolve("stale_workers")

        recent = store.get_recent(limit=10)
        assert len(recent) == 1
        assert recent[0]["resolved"] == 1

    def test_get_active_excludes_resolved(self, tmp_path):
        store = _make_alert_store(tmp_path)
        store.insert(Alert("error_spike", "Spike", "critical"))
        store.insert(Alert("stale_workers", "Stale", "warning"))
        store.resolve("error_spike")

        active = store.get_active()
        assert len(active) == 1
        assert active[0]["alert_type"] == "stale_workers"

    def test_get_recent_limit(self, tmp_path):
        store = _make_alert_store(tmp_path)
        for i in range(10):
            store.insert(Alert(f"type_{i}", f"msg_{i}", "warning"))

        recent = store.get_recent(limit=5)
        assert len(recent) == 5
        # Most recent first
        assert recent[0]["alert_type"] == "type_9"

    def test_empty_store(self, tmp_path):
        store = _make_alert_store(tmp_path)
        assert store.get_recent() == []
        assert store.get_active() == []

    def test_resolve_nonexistent_type(self, tmp_path):
        """Resolving a type that doesn't exist should not raise."""
        store = _make_alert_store(tmp_path)
        store.resolve("nonexistent_type")  # should not raise

    def test_resolve_only_active_of_type(self, tmp_path):
        """resolve() only marks unresolved alerts of the given type."""
        store = _make_alert_store(tmp_path)
        store.insert(Alert("error_spike", "First spike", "critical"))
        store.resolve("error_spike")
        # Insert another of the same type
        store.insert(Alert("error_spike", "Second spike", "critical"))

        recent = store.get_recent(limit=10)
        assert len(recent) == 2
        # First should be resolved, second should not
        assert recent[1]["resolved"] == 1  # older (id=1)
        assert recent[0]["resolved"] == 0  # newer (id=2)


# ── MetricsStore Tests ───────────────────────────────────────


class TestMetricsStore:
    """Test MetricsStore SQLite CRUD and retention."""

    def test_insert_and_get_trend(self, tmp_path):
        store = _make_metrics_store(tmp_path)
        now = int(time.time())
        s = MetricsSample(
            timestamp=now,
            events_per_minute=10.0,
            avg_duration_ms=5000.0,
            error_rate=0.1,
            cluster_utilization=50.0,
        )
        store.insert(s)

        trend = store.get_trend(minutes=60)
        assert len(trend) == 1
        assert trend[0].timestamp == now
        assert trend[0].events_per_minute == 10.0
        assert trend[0].avg_duration_ms == 5000.0

    def test_get_trend_time_range(self, tmp_path):
        store = _make_metrics_store(tmp_path)
        now = int(time.time())
        # Insert samples at different times
        store.insert(MetricsSample(timestamp=now - 600, events_per_minute=5.0))  # 10 min ago
        store.insert(MetricsSample(timestamp=now - 300, events_per_minute=10.0))  # 5 min ago
        store.insert(MetricsSample(timestamp=now, events_per_minute=15.0))  # now

        # Get last 3 minutes — should only get the most recent
        trend = store.get_trend(minutes=3)
        assert len(trend) == 1
        assert trend[0].events_per_minute == 15.0

        # Get last 7 minutes — should get the last two (5min ago + now)
        trend = store.get_trend(minutes=7)
        assert len(trend) == 2

        # Get last 15 minutes — should get all three
        trend = store.get_trend(minutes=15)
        assert len(trend) == 3

    def test_insert_or_replace(self, tmp_path):
        """INSERT OR REPLACE updates existing timestamp."""
        store = _make_metrics_store(tmp_path)
        now = int(time.time())
        store.insert(MetricsSample(timestamp=now, events_per_minute=5.0))
        store.insert(MetricsSample(timestamp=now, events_per_minute=10.0))

        trend = store.get_trend(minutes=60)
        assert len(trend) == 1
        assert trend[0].events_per_minute == 10.0

    def test_empty_store(self, tmp_path):
        store = _make_metrics_store(tmp_path)
        trend = store.get_trend(minutes=60)
        assert trend == []

    def test_retention_cleanup(self, tmp_path):
        """Old samples are cleaned up when retention threshold is met."""
        store = _make_metrics_store(tmp_path)
        now = int(time.time())
        # Insert an old sample (8 days ago — beyond default 7-day retention)
        old_ts = now - 8 * 86400
        # Make timestamp land near the hour mark to trigger cleanup
        old_ts = (old_ts // 3600) * 3600 + 30  # within 60s of hour mark
        store.insert(MetricsSample(timestamp=old_ts, events_per_minute=1.0))

        # Insert a new sample that triggers cleanup (timestamp near hour mark)
        new_ts = (now // 3600) * 3600 + 30
        store.insert(MetricsSample(timestamp=new_ts, events_per_minute=2.0))

        # Old sample should be cleaned up
        trend = store.get_trend(minutes=60 * 24 * 10)  # 10 days
        timestamps = [s.timestamp for s in trend]
        assert old_ts not in timestamps

    def test_wal_mode(self, tmp_path):
        """Database is created in WAL mode."""
        store = _make_metrics_store(tmp_path)
        conn = store._conn()
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"


# ── PrometheusExporter Tests ─────────────────────────────────


class TestPrometheusExporter:
    """Test PrometheusExporter (works with or without prometheus_client)."""

    def test_init_no_error(self):
        """PrometheusExporter initializes without error."""
        exporter = PrometheusExporter()
        # Either enabled (prometheus_client installed) or disabled
        assert isinstance(exporter.enabled, bool)

    def test_generate_returns_string(self):
        """generate() returns a string."""
        exporter = PrometheusExporter()
        output = exporter.generate()
        assert isinstance(output, str)
        if exporter.enabled:
            assert "uc_tasks_completed_total" in output
        else:
            assert "not installed" in output

    def test_record_event_no_error(self):
        """record_event() does not raise regardless of enabled state."""
        exporter = PrometheusExporter()
        # Should not raise
        exporter.record_event("task_completed", {"duration_ms": 5000})
        exporter.record_event("task_failed")
        exporter.record_event("subtask_retrying")
        exporter.record_event("unknown_event")

    def test_update_system_state_no_error(self):
        """update_system_state() does not raise regardless of enabled state."""
        exporter = PrometheusExporter()
        exporter.update_system_state(
            circuit_breaker_state="open",
            rate_limiter_remaining=0.5,
            cluster_utilization_pct=75.0,
        )

    def test_circuit_breaker_state_mapping(self):
        """Circuit breaker state maps to correct numeric values."""
        exporter = PrometheusExporter()
        if not exporter.enabled:
            pytest.skip("prometheus_client not installed")

        exporter.update_system_state(circuit_breaker_state="closed")
        assert exporter.circuit_breaker_state._value.get() == 0

        exporter.update_system_state(circuit_breaker_state="half_open")
        assert exporter.circuit_breaker_state._value.get() == 0.5

        exporter.update_system_state(circuit_breaker_state="open")
        assert exporter.circuit_breaker_state._value.get() == 1

    def test_unknown_cb_state_maps_to_negative(self):
        """Unknown circuit breaker state maps to -1."""
        exporter = PrometheusExporter()
        if not exporter.enabled:
            pytest.skip("prometheus_client not installed")

        exporter.update_system_state(circuit_breaker_state="unknown_state")
        assert exporter.circuit_breaker_state._value.get() == -1

    def test_cluster_utilization_conversion(self):
        """Cluster utilization is converted from percentage to ratio."""
        exporter = PrometheusExporter()
        if not exporter.enabled:
            pytest.skip("prometheus_client not installed")

        exporter.update_system_state(cluster_utilization_pct=75.0)
        assert abs(exporter.cluster_utilization._value.get() - 0.75) < 0.001


# ── check_alerts Tests ───────────────────────────────────────


class TestCheckAlerts:
    """Test MetricsAggregator.check_alerts() with all 7 conditions."""

    def test_no_alerts_on_healthy_snapshot(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        snap = agg.snapshot()
        new_alerts, resolved = agg.check_alerts(snap)
        assert new_alerts == []
        assert resolved == []

    def test_error_spike_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import (
            EventMetrics,
            MetricsSnapshot,
        )
        snap = MetricsSnapshot(
            event=EventMetrics(error_spike=True),
        )
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "error_spike"
        assert new_alerts[0].severity == "critical"

    def test_error_spike_disabled(self, tmp_path):
        cfg = AlertConfig(error_spike_alert=False)
        agg = _make_aggregator(tmp_path, alert_config=cfg)
        from ultimate_coders.dashboard.metrics import EventMetrics, MetricsSnapshot
        snap = MetricsSnapshot(event=EventMetrics(error_spike=True))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 0

    def test_slow_tasks_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(slow_tasks_count=3))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "slow_tasks"
        assert new_alerts[0].severity == "warning"

    def test_high_latency_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(p95_duration_ms=400_000))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "high_latency"

    def test_high_latency_below_threshold(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(p95_duration_ms=200_000))
        new_alerts, _ = agg.check_alerts(snap)
        # No high_latency alert (200s < 300s threshold)
        alert_types = [a.alert_type for a in new_alerts]
        assert "high_latency" not in alert_types

    def test_circuit_breaker_open_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, SystemMetrics
        snap = MetricsSnapshot(system=SystemMetrics(circuit_breaker_state="open"))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "circuit_breaker_open"
        assert new_alerts[0].severity == "critical"

    def test_rate_limiter_high_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, SystemMetrics
        snap = MetricsSnapshot(system=SystemMetrics(rate_limiter_remaining_ratio=0.1))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "rate_limiter_high"

    def test_stale_workers_alert(self, tmp_path):
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, WorkerMetrics
        snap = MetricsSnapshot(worker=WorkerMetrics(avg_heartbeat_age_seconds=200.0))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "stale_workers"

    def test_recent_failures_alert(self, tmp_path):
        """Recent failures alert uses window-based count, not cumulative."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(recent_failed=5))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "recent_failures"
        assert new_alerts[0].severity == "critical"

    def test_recent_failures_below_threshold(self, tmp_path):
        """Recent failures below threshold does not trigger alert."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(recent_failed=3))
        # 3 < 5 threshold, but recent_failed > 0 means type is "current"
        new_alerts, _ = agg.check_alerts(snap)
        alert_types = [a.alert_type for a in new_alerts]
        # Should NOT trigger because 3 < 5
        assert "recent_failures" not in alert_types

    def test_alert_dedup(self, tmp_path):
        """Same alert type is not triggered twice in a row."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import EventMetrics, MetricsSnapshot
        snap = MetricsSnapshot(event=EventMetrics(error_spike=True))

        new1, _ = agg.check_alerts(snap)
        assert len(new1) == 1

        # Second check — same condition still active
        new2, _ = agg.check_alerts(snap)
        assert len(new2) == 0  # deduped

    def test_alert_resolve(self, tmp_path):
        """Alert resolves when condition clears."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import EventMetrics, MetricsSnapshot

        # Trigger
        snap_bad = MetricsSnapshot(event=EventMetrics(error_spike=True))
        new1, resolved1 = agg.check_alerts(snap_bad)
        assert len(new1) == 1
        assert len(resolved1) == 0

        # Resolve
        snap_ok = MetricsSnapshot(event=EventMetrics(error_spike=False))
        new2, resolved2 = agg.check_alerts(snap_ok)
        assert len(new2) == 0
        assert "error_spike" in resolved2

    def test_alert_persisted_to_sqlite(self, tmp_path):
        """Triggered alerts are persisted to the AlertStore."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import EventMetrics, MetricsSnapshot
        snap = MetricsSnapshot(event=EventMetrics(error_spike=True))
        agg.check_alerts(snap)

        recent = agg.alert_store.get_recent(limit=10)
        assert len(recent) == 1
        assert recent[0]["alert_type"] == "error_spike"

    def test_resolved_alert_marked_in_sqlite(self, tmp_path):
        """Resolved alerts are marked in the AlertStore."""
        agg = _make_aggregator(tmp_path)
        from ultimate_coders.dashboard.metrics import EventMetrics, MetricsSnapshot

        snap_bad = MetricsSnapshot(event=EventMetrics(error_spike=True))
        agg.check_alerts(snap_bad)

        snap_ok = MetricsSnapshot(event=EventMetrics(error_spike=False))
        agg.check_alerts(snap_ok)

        recent = agg.alert_store.get_recent(limit=10)
        assert len(recent) == 1
        assert recent[0]["resolved"] == 1

    def test_custom_failure_threshold(self, tmp_path):
        """Custom failure_count_threshold is respected."""
        cfg = AlertConfig(failure_count_threshold=2)
        agg = _make_aggregator(tmp_path, alert_config=cfg)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        snap = MetricsSnapshot(task=TaskMetrics(recent_failed=2))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "recent_failures"

    def test_custom_latency_threshold(self, tmp_path):
        """Custom high_latency_ms threshold is respected."""
        cfg = AlertConfig(high_latency_ms=100_000.0)  # 100s
        agg = _make_aggregator(tmp_path, alert_config=cfg)
        from ultimate_coders.dashboard.metrics import MetricsSnapshot, TaskMetrics
        # 150s > 100s custom threshold
        snap = MetricsSnapshot(task=TaskMetrics(p95_duration_ms=150_000))
        new_alerts, _ = agg.check_alerts(snap)
        assert len(new_alerts) == 1
        assert new_alerts[0].alert_type == "high_latency"


# ── MetricsAggregator Integration Tests ──────────────────────


class TestMetricsAggregatorIntegration:
    """Test MetricsAggregator with record_event and snapshot."""

    def test_snapshot_includes_recent_failed(self, tmp_path):
        """snapshot() includes recent_failed count from sliding window."""
        agg = _make_aggregator(tmp_path)
        agg.record_event("task_failed")
        agg.record_event("task_failed")
        snap = agg.snapshot()
        assert snap.task.recent_failed == 2
        assert snap.task.total_failed == 2

    def test_recent_failed_zero_when_no_failures(self, tmp_path):
        """recent_failed is 0 when no events are in the window."""
        agg = _make_aggregator(tmp_path)
        snap = agg.snapshot()
        assert snap.task.recent_failed == 0

    def test_generate_prometheus_returns_string(self, tmp_path):
        """generate_prometheus() returns a string."""
        agg = _make_aggregator(tmp_path)
        output = agg.generate_prometheus()
        assert isinstance(output, str)

    def test_get_trend_from_sqlite(self, tmp_path):
        """get_trend() reads from SQLite."""
        agg = _make_aggregator(tmp_path)
        # Insert a sample directly
        now = int(time.time())
        agg._metrics_store.insert(MetricsSample(timestamp=now, events_per_minute=5.0))

        trend = agg.get_trend(minutes=60)
        assert len(trend) >= 1
        assert trend[-1].events_per_minute == 5.0
