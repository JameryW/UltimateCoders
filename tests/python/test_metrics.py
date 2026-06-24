"""Unit tests for MetricsAggregator — sliding-window metrics engine."""

from __future__ import annotations

import time

import pytest
from ultimate_coders.dashboard.metrics import (
    WINDOW_SECONDS,
    MetricsAggregator,
    MetricsSnapshot,
    _percentile,
)

# ── Helpers ────────────────────────────────────────────────

def _record_events(agg: MetricsAggregator, events: list[tuple[str, dict | None]]) -> None:
    """Record a batch of events on the aggregator."""
    for event_type, data in events:
        agg.record_event(event_type, data)


def _completed_event(duration_ms: float, worker_id: str = "w1") -> tuple[str, dict]:
    return ("subtask_completed", {"duration_ms": duration_ms, "worker_id": worker_id})


def _failed_event(worker_id: str = "w1") -> tuple[str, dict]:
    return ("subtask_failed", {"worker_id": worker_id})


# ── _percentile ────────────────────────────────────────────

class TestPercentile:
    def test_empty(self) -> None:
        assert _percentile([], 95) == 0.0

    def test_single(self) -> None:
        assert _percentile([100.0], 50) == 100.0

    def test_p50(self) -> None:
        assert _percentile([10, 20, 30, 40, 50], 50) == 30.0

    def test_p95_interpolation(self) -> None:
        vals = list(range(1, 21))  # 1..20
        result = _percentile(vals, 95)
        assert 18.0 <= result <= 20.0

    def test_p99(self) -> None:
        vals = list(range(1, 101))
        result = _percentile(vals, 99)
        assert result >= 98.0


# ── MetricsAggregator ─────────────────────────────────────

class TestMetricsAggregator:
    def test_empty_snapshot(self) -> None:
        agg = MetricsAggregator()
        snap = agg.snapshot()
        assert isinstance(snap, MetricsSnapshot)
        assert snap.task.avg_duration_ms == 0.0
        assert snap.task.p95_duration_ms == 0.0
        assert snap.task.total_completed == 0
        assert snap.event.events_per_minute == 0.0
        assert not snap.event.error_spike

    def test_record_completed_task(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(5000),
            _completed_event(10000),
            _completed_event(15000),
        ])
        snap = agg.snapshot()
        assert snap.task.total_completed == 3
        assert snap.task.avg_duration_ms == 10000.0
        assert snap.task.p50_duration_ms > 0
        assert snap.task.p95_duration_ms > 0

    def test_failed_tasks_counted(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(1000),
            _failed_event(),
            _failed_event(),
        ])
        snap = agg.snapshot()
        assert snap.task.total_completed == 1
        assert snap.task.total_failed == 2
        # success_rate = 1/3
        assert abs(snap.task.success_rate - 1/3) < 0.01

    def test_retry_rate(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(1000),
            _completed_event(2000),
            ("subtask_retrying", {"worker_id": "w1"}),
        ])
        snap = agg.snapshot()
        # retry_rate = retries / (completed + failed) = 1 / 2
        assert snap.task.retry_rate == pytest.approx(0.5, abs=0.01)

    def test_slow_tasks_count(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(1000),
            _completed_event(400_000),  # > 5min threshold
            _completed_event(600_000),  # > 5min threshold
        ])
        snap = agg.snapshot()
        assert snap.task.slow_tasks_count == 2

    def test_worker_tool_calls(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            ("tool_call", {"worker_id": "w1"}),
            ("tool_call", {"worker_id": "w1"}),
            ("tool_call", {"worker_id": "w2"}),
        ])
        snap = agg.snapshot()
        assert snap.worker.per_worker_tool_calls == {"w1": 2, "w2": 1}

    def test_worker_subtask_counts(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(1000, "w1"),
            _completed_event(2000, "w1"),
            _failed_event("w2"),
        ])
        snap = agg.snapshot()
        assert snap.worker.per_worker_subtask_count == {"w1": 2, "w2": 1}

    def test_event_type_counts(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            ("tool_call", None),
            ("tool_call", None),
            ("subtask_completed", {"duration_ms": 100}),
        ])
        snap = agg.snapshot()
        assert snap.event.event_type_counts.get("tool_call") == 2
        assert snap.event.event_type_counts.get("subtask_completed") == 1

    def test_events_per_minute(self) -> None:
        agg = MetricsAggregator()
        # Record 10 events — should give ~10 * (60/rate_window) events/min
        for i in range(10):
            agg.record_event("task_completed", {"duration_ms": 1000})
        snap = agg.snapshot()
        # With 10 events in a very short window, events_per_minute should be high
        assert snap.event.events_per_minute > 0

    def test_error_spike_detected(self) -> None:
        agg = MetricsAggregator()
        # Record enough failures to trigger spike (>30% with >=3 total outcomes)
        _record_events(agg, [
            _completed_event(1000),
            _failed_event(),
            _failed_event(),
            _failed_event(),
        ])
        snap = agg.snapshot()
        # 3 failed / 4 total = 75% > 30% and total >= 3
        assert snap.event.error_spike

    def test_error_spike_not_triggered_below_threshold(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _completed_event(1000),
            _completed_event(2000),
            _completed_event(3000),
            _failed_event(),
        ])
        snap = agg.snapshot()
        # 1/4 = 25% < 30% — no spike
        assert not snap.event.error_spike

    def test_error_spike_not_triggered_too_few_outcomes(self) -> None:
        agg = MetricsAggregator()
        _record_events(agg, [
            _failed_event(),
            _failed_event(),
        ])
        snap = agg.snapshot()
        # 2 outcomes < 3 minimum — no spike even if rate is high
        assert not snap.event.error_spike

    def test_update_system_state(self) -> None:
        agg = MetricsAggregator()
        agg.update_system_state(
            circuit_breaker_state="open",
            rate_limiter_remaining=0.5,
            cluster_utilization_pct=75.0,
            avg_heartbeat_age=2.3,
        )
        snap = agg.snapshot()
        assert snap.system.circuit_breaker_state == "open"
        assert snap.system.rate_limiter_remaining_ratio == 0.5
        assert snap.system.cluster_utilization_pct == 75.0
        assert snap.worker.avg_heartbeat_age_seconds == 2.3

    def test_uptime_increases(self) -> None:
        agg = MetricsAggregator()
        snap1 = agg.snapshot()
        time.sleep(0.1)
        snap2 = agg.snapshot()
        assert snap2.system.uptime_seconds >= snap1.system.uptime_seconds

    def test_trend_sampling(self) -> None:
        agg = MetricsAggregator()
        # First snapshot should produce a trend sample
        agg.record_event("task_completed", {"duration_ms": 1000})
        snap = agg.snapshot()
        assert len(snap.trend) == 1
        assert snap.trend[0].timestamp > 0

    def test_trend_not_sampled_too_frequently(self) -> None:
        agg = MetricsAggregator()
        agg.record_event("task_completed", {"duration_ms": 1000})
        snap1 = agg.snapshot()
        snap2 = agg.snapshot()  # Called immediately — should NOT add another sample
        assert len(snap2.trend) == len(snap1.trend)

    def test_sliding_window_expiry(self) -> None:
        agg = MetricsAggregator()
        # Inject an event with an old timestamp
        agg.record_event("task_completed", {"duration_ms": 5000})
        # Manually backdate the event
        with agg._lock:
            for e in agg._events:
                e.ts = time.monotonic() - WINDOW_SECONDS - 1
        # Snapshot should purge expired events
        snap = agg.snapshot()
        assert snap.task.total_completed == 1  # Cumulative counter doesn't expire
        assert snap.task.avg_duration_ms == 0.0  # But sliding-window data is gone

    def test_duration_ms_invalid_ignored(self) -> None:
        agg = MetricsAggregator()
        agg.record_event("subtask_completed", {"duration_ms": "not_a_number"})
        agg.record_event("subtask_completed", {"duration_ms": -5})
        snap = agg.snapshot()
        # Invalid durations should be ignored
        assert snap.task.total_completed == 2
        assert snap.task.avg_duration_ms == 0.0  # No valid durations

    def test_snapshot_thread_safety(self) -> None:
        """Rapid interleaved record + snapshot should not crash."""
        import threading
        agg = MetricsAggregator()
        errors: list[Exception] = []

        def writer() -> None:
            try:
                for i in range(100):
                    agg.record_event("task_completed", {"duration_ms": i * 100, "worker_id": "w1"})
            except Exception as e:
                errors.append(e)

        def reader() -> None:
            try:
                for _ in range(50):
                    agg.snapshot()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snap = agg.snapshot()
        assert snap.task.total_completed == 100
