"""The Dashboard API no longer exposes the OMP terminal bridge."""

from unittest.mock import MagicMock

from ultimate_coders.dashboard.app import DashboardApp


def test_dashboard_routes_exclude_tui_websocket() -> None:
    app = DashboardApp(orchestrator=MagicMock())
    paths = {route.path for route in app._app.routes}

    assert "/ws/tui" not in paths
    assert "/dashboard/api/health" in paths
