"""Dashboard-owned resources are released exactly once on shutdown."""

from unittest.mock import MagicMock

from ultimate_coders.dashboard.app import DashboardApp


def test_stop_closes_metrics_and_is_idempotent(monkeypatch):
    metrics = MagicMock()
    monkeypatch.setattr(
        "ultimate_coders.dashboard.app.MetricsAggregator",
        lambda: metrics,
    )
    app = DashboardApp(orchestrator=None)

    app.stop()
    app.stop()

    metrics.close.assert_called_once_with()
