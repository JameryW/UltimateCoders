"""Scheduler — Python wrapper around the Rust SchedulerService.

Provides a high-level Python API for task scheduling with night-time
orchestration support. Wraps the PyO3 `SchedulerService` class from
`ultimate_coders._uc_core`.

Usage:
    from ultimate_coders.agent.scheduler import Scheduler

    scheduler = Scheduler()
    scheduler.set_night_window("22:00", "06:00", "Asia/Shanghai")

    # Create a cron job
    task = scheduler.create_cron_job(
        "Rebuild search index",
        "0 22 * * *",
        project_id="project-alpha",
    )

    # Create a one-shot job
    task = scheduler.create_one_shot_job(
        "Run code review",
        "2024-01-15T22:00:00Z",
        project_id="project-beta",
    )

    scheduler.start()

    # List all jobs
    jobs = scheduler.list_jobs()

    # Cancel a job
    scheduler.cancel_job(task.id)

    # Get execution history
    history = scheduler.get_execution_history(task.id)

    scheduler.stop()
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ultimate_coders.agent.scheduler_config import (
    SchedulerConfig,
    load_scheduler_config,
)

if TYPE_CHECKING:
    from ultimate_coders._uc_core import PyScheduledTask as ScheduledTask

try:
    from ultimate_coders._uc_core import PySchedulerService as _PySchedulerService
except ImportError:  # pragma: no cover
    _PySchedulerService = None  # Rust extension not built yet


class Scheduler:
    """Python scheduler interface for task scheduling and night-time orchestration.

    Wraps the Rust `SchedulerService` via PyO3 FFI. Provides methods for
    creating cron-based and one-shot scheduled tasks, managing the night
    execution window, and querying execution history.
    """

    def __init__(self) -> None:
        """Create a new Scheduler instance.

        Raises:
            ImportError: If the Rust extension is not built.
        """
        if _PySchedulerService is None:
            raise ImportError(
                "Rust extension not built. Run `maturin develop` first."
            )
        self._service = _PySchedulerService()

    def create_cron_job(
        self,
        description: str,
        cron_expression: str,
        *,
        project_id: str | None = None,
        night_window_start: str | None = None,
        night_window_end: str | None = None,
        timezone: str = "UTC",
    ) -> ScheduledTask:
        """Create a cron-based recurring job.

        Args:
            description: Human-readable description of the task.
            cron_expression: Standard cron expression (e.g., "0 22 * * *").
            project_id: Project/repository context (default: "").
            night_window_start: Night window start time in HH:MM (default: "22:00").
            night_window_end: Night window end time in HH:MM (default: "06:00").
            timezone: IANA timezone name (default: "UTC").

        Returns:
            ScheduledTask with the created task details.
        """
        return self._service.create_cron_job(
            description,
            cron_expression,
            project_id,
            night_window_start,
            night_window_end,
            timezone,
        )

    def create_one_shot_job(
        self,
        description: str,
        execute_after: datetime | str,
        *,
        project_id: str | None = None,
        night_window_start: str | None = None,
        night_window_end: str | None = None,
        timezone: str = "UTC",
    ) -> ScheduledTask:
        """Create a one-shot delayed job.

        Args:
            description: Human-readable description of the task.
            execute_after: When to execute the task. Can be a datetime object
                or an ISO 8601 string (e.g., "2024-01-15T22:00:00Z").
            project_id: Project/repository context (default: "").
            night_window_start: Night window start time in HH:MM (default: "22:00").
            night_window_end: Night window end time in HH:MM (default: "06:00").
            timezone: IANA timezone name (default: "UTC").

        Returns:
            ScheduledTask with the created task details.
        """
        if isinstance(execute_after, datetime):
            execute_after_str = execute_after.isoformat()
        else:
            execute_after_str = str(execute_after)

        return self._service.create_one_shot_job(
            description,
            execute_after_str,
            project_id,
            night_window_start,
            night_window_end,
            timezone,
        )

    def cancel_job(self, task_id: str) -> bool:
        """Cancel (remove) a scheduled job.

        Args:
            task_id: The UUID string of the task to cancel.

        Returns:
            True if the job was successfully cancelled.
        """
        return self._service.cancel_job(task_id)

    def list_jobs(self) -> list[Any]:
        """List all registered jobs.

        Returns:
            List of ScheduledTask objects.
        """
        return self._service.list_jobs()

    def get_job(self, task_id: str) -> Any | None:
        """Get a specific job by ID.

        Args:
            task_id: The UUID string of the task.

        Returns:
            ScheduledTask if found, None otherwise.
        """
        return self._service.get_job(task_id)

    def get_execution_history(self, task_id: str, limit: int = 50) -> list[Any]:
        """Get execution history for a specific task.

        Args:
            task_id: The UUID string of the task.
            limit: Maximum number of records to return (default: 50).

        Returns:
            List of ExecutionHistory objects.
        """
        return self._service.get_execution_history(task_id, limit)

    def set_night_window(
        self,
        start_time: str,
        end_time: str,
        timezone: str = "UTC",
    ) -> None:
        """Set the night window configuration.

        Args:
            start_time: Night window start time in HH:MM (e.g., "22:00").
            end_time: Night window end time in HH:MM (e.g., "06:00").
            timezone: IANA timezone name (default: "UTC").
        """
        self._service.set_night_window(start_time, end_time, timezone)

    def trigger_job(self, task_id: str) -> bool:
        """Manually trigger a scheduled job for immediate execution.

        This is a convenience method for the dashboard to manually
        trigger a job outside its normal schedule. It looks up the
        job by ID and creates a one-shot duplicate that executes now.

        Args:
            task_id: The UUID string of the scheduled task to trigger.

        Returns:
            True if the job was found and triggered, False otherwise.
        """
        job = self.get_job(task_id)
        if job is None:
            return False

        description = getattr(job, "description", str(task_id))
        project_id = getattr(job, "project_id", None)

        # Create a one-shot job that executes immediately (now + 1s)
        from datetime import datetime as _dt, timezone as _tz
        execute_after = _dt.now(_tz.utc).isoformat()

        try:
            self.create_one_shot_job(
                description,
                execute_after,
                project_id=project_id,
            )
            return True
        except Exception:
            return False

    def clear_night_window(self) -> None:
        """Clear the night window configuration (allow execution at any time)."""
        self._service.clear_night_window()

    def start(self) -> None:
        """Start the scheduler.

        Loads persisted tasks and begins the scheduling loop.
        """
        self._service.start()

    def stop(self) -> None:
        """Stop the scheduler."""
        self._service.stop()

    def is_running(self) -> bool:
        """Whether the scheduler is currently running.

        Returns:
            True if the scheduler is running.
        """
        return self._service.is_running()

    def load_config(self, path: str) -> SchedulerConfig:
        """Load a YAML configuration file and register all tasks.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            The loaded SchedulerConfig.
        """
        config = load_scheduler_config(path)

        # Set night window if configured
        if config.night_window:
            self.set_night_window(
                config.night_window.start,
                config.night_window.end,
                config.night_window.timezone,
            )

        # Register each task
        for task_config in config.tasks:
            if task_config.cron_expression:
                self.create_cron_job(
                    task_config.description,
                    task_config.cron_expression,
                    project_id=task_config.project_id,
                    night_window_start=task_config.night_window_start,
                    night_window_end=task_config.night_window_end,
                    timezone=task_config.timezone,
                )
            elif task_config.execute_after:
                self.create_one_shot_job(
                    task_config.description,
                    task_config.execute_after,
                    project_id=task_config.project_id,
                    night_window_start=task_config.night_window_start,
                    night_window_end=task_config.night_window_end,
                    timezone=task_config.timezone,
                )

        return config
