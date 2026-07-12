"""Dashboard — embedded Web UI for monitoring cluster runtime status.

Provides a FastAPI-based web dashboard embedded in the Orchestrator process.
Monitors engine health, worker load, task execution, and scheduler status.
Uses SSE for real-time updates.

Usage:
    from ultimate_coders.dashboard import DashboardApp

    app = DashboardApp(orchestrator)
    app.start(host="0.0.0.0", port=8080)
"""

from ultimate_coders.dashboard.app import DashboardApp

__all__ = ["DashboardApp"]
