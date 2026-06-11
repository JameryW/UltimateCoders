"""Unit tests for the scheduler module.

Tests the Python Scheduler class and config loading, using the
InMemoryScheduleStore (no external infrastructure required).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from ultimate_coders.agent.scheduler import Scheduler
from ultimate_coders.agent.scheduler_config import (
    SchedulerConfig,
    load_scheduler_config,
)

# ── Scheduler tests ────────────────────────────────────────────────

class TestScheduler:
    """Tests for the Scheduler class."""

    @pytest.fixture
    def scheduler(self):
        """Create a fresh Scheduler instance for each test."""
        s = Scheduler()
        yield s
        # Clean up: stop if running
        try:
            if s.is_running():
                s.stop()
        except Exception:
            pass

    def test_scheduler_construction(self):
        """Scheduler should construct successfully."""
        s = Scheduler()
        assert s is not None
        assert not s.is_running()

    def test_create_cron_job(self, scheduler):
        """Creating a cron job should return a ScheduledTask."""
        task = scheduler.create_cron_job(
            "Rebuild index",
            "0 22 * * *",
            project_id="project-alpha",
        )
        assert task is not None
        assert task.description == "Rebuild index"
        assert task.project_id == "project-alpha"
        assert task.cron_expression == "0 22 * * *"
        assert task.is_cron()
        assert not task.is_one_shot()

    def test_create_cron_job_with_night_window(self, scheduler):
        """Cron job should accept custom night window parameters."""
        task = scheduler.create_cron_job(
            "Rebuild index",
            "0 22 * * *",
            project_id="project-alpha",
            night_window_start="20:00",
            night_window_end="04:00",
            timezone="Asia/Shanghai",
        )
        assert task.night_window_start == "20:00"
        assert task.night_window_end == "04:00"
        assert task.timezone == "Asia/Shanghai"

    def test_create_one_shot_job_with_string(self, scheduler):
        """Creating a one-shot job with a string timestamp."""
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        task = scheduler.create_one_shot_job(
            "Run review",
            future.isoformat(),
            project_id="project-beta",
        )
        assert task is not None
        assert task.description == "Run review"
        assert task.project_id == "project-beta"
        assert task.is_one_shot()
        assert not task.is_cron()

    def test_create_one_shot_job_with_datetime(self, scheduler):
        """Creating a one-shot job with a datetime object."""
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        task = scheduler.create_one_shot_job(
            "Run review",
            future,
            project_id="project-beta",
        )
        assert task is not None
        assert task.description == "Run review"
        assert task.is_one_shot()

    def test_create_one_shot_job_with_iso_string(self, scheduler):
        """Creating a one-shot job with an ISO 8601 string."""
        task = scheduler.create_one_shot_job(
            "Run review",
            "2024-12-31T22:00:00Z",
            project_id="project-beta",
        )
        assert task is not None
        assert task.description == "Run review"

    def test_list_jobs(self, scheduler):
        """list_jobs should return all registered tasks."""
        scheduler.create_cron_job(
            "Task 1", "0 22 * * *", project_id="p1"
        )
        scheduler.create_cron_job(
            "Task 2", "0 23 * * *", project_id="p2"
        )
        jobs = scheduler.list_jobs()
        assert len(jobs) == 2
        descriptions = {j.description for j in jobs}
        assert "Task 1" in descriptions
        assert "Task 2" in descriptions

    def test_get_job(self, scheduler):
        """get_job should retrieve a task by ID."""
        task = scheduler.create_cron_job(
            "Task to retrieve", "0 22 * * *", project_id="p1"
        )
        retrieved = scheduler.get_job(task.id)
        assert retrieved is not None
        assert retrieved.description == "Task to retrieve"
        assert retrieved.id == task.id

    def test_get_job_not_found(self, scheduler):
        """get_job should return None for non-existent task."""
        result = scheduler.get_job("00000000-0000-0000-0000-000000000000")
        assert result is None

    def test_cancel_job(self, scheduler):
        """cancel_job should remove a task."""
        task = scheduler.create_cron_job(
            "Task to cancel", "0 22 * * *", project_id="p1"
        )
        assert len(scheduler.list_jobs()) == 1

        result = scheduler.cancel_job(task.id)
        assert result is True
        assert len(scheduler.list_jobs()) == 0

    def test_cancel_job_invalid_uuid(self, scheduler):
        """cancel_job should raise ValueError for invalid UUID."""
        with pytest.raises(ValueError, match="Invalid UUID"):
            scheduler.cancel_job("not-a-uuid")

    def test_set_night_window(self, scheduler):
        """set_night_window should configure the night window."""
        scheduler.set_night_window("22:00", "06:00", "UTC")
        # Night window is set internally; no direct getter exposed
        # Verify it doesn't raise

    def test_set_night_window_invalid_time(self, scheduler):
        """set_night_window should raise ValueError for invalid time."""
        with pytest.raises(ValueError, match="Invalid time"):
            scheduler.set_night_window("25:00", "06:00", "UTC")

    def test_clear_night_window(self, scheduler):
        """clear_night_window should not raise."""
        scheduler.set_night_window("22:00", "06:00", "UTC")
        scheduler.clear_night_window()
        # No direct assertion; verify it doesn't raise

    def test_start_stop(self, scheduler):
        """start and stop should toggle is_running."""
        assert not scheduler.is_running()
        scheduler.start()
        assert scheduler.is_running()
        scheduler.stop()
        assert not scheduler.is_running()

    def test_execution_history(self, scheduler):
        """Execution history should be queryable."""
        task = scheduler.create_cron_job(
            "Task with history", "0 22 * * *", project_id="p1"
        )
        # Initially no history
        history = scheduler.get_execution_history(task.id)
        assert isinstance(history, list)

    def test_start_recovers_persisted_tasks(self, scheduler):
        """start should recover tasks from the store."""
        scheduler.create_cron_job(
            "Task 1", "0 22 * * *", project_id="p1"
        )
        scheduler.create_one_shot_job(
            "Task 2", "2025-01-01T00:00:00Z", project_id="p2"
        )
        assert len(scheduler.list_jobs()) == 2

        # Stop and restart
        scheduler.stop()
        assert not scheduler.is_running()

        # Create a new scheduler (simulating restart with same store)
        # Note: InMemory store is per-instance, so this test verifies
        # the internal state is maintained within the same instance
        scheduler.start()
        assert scheduler.is_running()
        # Jobs should still be there
        assert len(scheduler.list_jobs()) == 2


# ── SchedulerConfig tests ──────────────────────────────────────────

class TestSchedulerConfig:
    """Tests for scheduler configuration parsing."""

    def test_parse_night_window_config(self):
        """Parsing a valid night window config."""
        config = _write_and_load_yaml(
            """
night_window:
  start: "22:00"
  end: "06:00"
  timezone: "Asia/Shanghai"

tasks: []
"""
        )
        assert config.night_window is not None
        assert config.night_window.start == "22:00"
        assert config.night_window.end == "06:00"
        assert config.night_window.timezone == "Asia/Shanghai"

    def test_parse_cron_task(self):
        """Parsing a cron task entry."""
        config = _write_and_load_yaml(
            """
tasks:
  - description: "Rebuild index"
    cron_expression: "0 22 * * *"
    project_id: "project-alpha"
"""
        )
        assert len(config.tasks) == 1
        task = config.tasks[0]
        assert task.description == "Rebuild index"
        assert task.cron_expression == "0 22 * * *"
        assert task.project_id == "project-alpha"
        assert task.timezone == "UTC"
        assert task.enabled is True

    def test_parse_one_shot_task(self):
        """Parsing a one-shot task entry."""
        config = _write_and_load_yaml(
            """
tasks:
  - description: "Run review"
    execute_after: "2024-01-15T22:00:00Z"
    project_id: "project-beta"
"""
        )
        assert len(config.tasks) == 1
        task = config.tasks[0]
        assert task.description == "Run review"
        assert task.execute_after == "2024-01-15T22:00:00Z"
        assert task.project_id == "project-beta"
        assert task.cron_expression is None

    def test_parse_task_with_custom_timezone(self):
        """Parsing a task with custom timezone."""
        config = _write_and_load_yaml(
            """
tasks:
  - description: "Task"
    cron_expression: "0 22 * * *"
    timezone: "America/New_York"
"""
        )
        assert config.tasks[0].timezone == "America/New_York"

    def test_parse_task_disabled(self):
        """Parsing a disabled task."""
        config = _write_and_load_yaml(
            """
tasks:
  - description: "Disabled task"
    cron_expression: "0 22 * * *"
    enabled: false
"""
        )
        assert config.tasks[0].enabled is False

    def test_parse_multiple_tasks(self):
        """Parsing multiple tasks."""
        config = _write_and_load_yaml(
            """
night_window:
  start: "22:00"
  end: "06:00"
  timezone: "UTC"

tasks:
  - description: "Task 1"
    cron_expression: "0 22 * * *"
    project_id: "p1"

  - description: "Task 2"
    cron_expression: "0 23 * * *"
    project_id: "p2"

  - description: "Task 3"
    execute_after: "2024-01-15T22:00:00Z"
"""
        )
        assert config.night_window is not None
        assert config.night_window.timezone == "UTC"
        assert len(config.tasks) == 3
        assert config.tasks[0].description == "Task 1"
        assert config.tasks[1].description == "Task 2"
        assert config.tasks[2].description == "Task 3"

    def test_validation_missing_description(self):
        """Empty description should raise ValueError."""
        with pytest.raises(ValueError):
            _write_and_load_yaml(
                """
tasks:
  - description: ""
    cron_expression: "0 22 * * *"
"""
            )

    def test_validation_both_cron_and_execute_after(self):
        """Specifying both cron and execute_after should raise ValueError."""
        with pytest.raises(ValueError, match="cannot specify both"):
            _write_and_load_yaml(
                """
tasks:
  - description: "Bad task"
    cron_expression: "0 22 * * *"
    execute_after: "2024-01-15T22:00:00Z"
"""
            )

    def test_validation_neither_cron_nor_execute_after(self):
        """Specifying neither cron nor execute_after should raise ValueError."""
        with pytest.raises(ValueError, match="must specify either"):
            _write_and_load_yaml(
                """
tasks:
  - description: "Bad task"
"""
            )

    def test_validation_invalid_time_format(self):
        """Invalid time format should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid night_window.start"):
            _write_and_load_yaml(
                """
night_window:
  start: "not-a-time"
  end: "06:00"

tasks: []
"""
            )

    def test_validation_invalid_task_time(self):
        """Invalid task night_window_start should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid night_window_start"):
            _write_and_load_yaml(
                """
tasks:
  - description: "Bad task"
    cron_expression: "0 22 * * *"
    night_window_start: "not-a-time"
"""
            )

    def test_empty_yaml(self):
        """Empty YAML should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            _write_and_load_yaml("")

    def test_tasks_must_be_list(self):
        """tasks must be a list."""
        with pytest.raises(ValueError, match="must be a list"):
            _write_and_load_yaml(
                """
tasks:
  description: "Not a list"
"""
            )


# ── Scheduler.load_config tests ────────────────────────────────────

class TestSchedulerLoadConfig:
    """Tests for Scheduler.load_config()."""

    def test_load_config_sets_night_window(self):
        """load_config should set the night window."""
        scheduler = Scheduler()
        path = _write_temp_yaml(
            """
night_window:
  start: "21:00"
  end: "05:00"
  timezone: "Asia/Shanghai"

tasks: []
"""
        )
        config = scheduler.load_config(path)
        assert config.night_window is not None
        assert config.night_window.start == "21:00"

    def test_load_config_creates_cron_jobs(self):
        """load_config should create cron jobs from YAML."""
        scheduler = Scheduler()
        path = _write_temp_yaml(
            """
tasks:
  - description: "Rebuild index"
    cron_expression: "0 22 * * *"
    project_id: "project-alpha"
"""
        )
        scheduler.load_config(path)
        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].description == "Rebuild index"

    def test_load_config_creates_one_shot_jobs(self):
        """load_config should create one-shot jobs from YAML."""
        scheduler = Scheduler()
        path = _write_temp_yaml(
            """
tasks:
  - description: "Run review"
    execute_after: "2025-01-01T00:00:00Z"
    project_id: "project-beta"
"""
        )
        scheduler.load_config(path)
        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].is_one_shot()

    def test_load_config_mixed_tasks(self):
        """load_config should handle mixed cron and one-shot tasks."""
        scheduler = Scheduler()
        path = _write_temp_yaml(
            """
night_window:
  start: "22:00"
  end: "06:00"
  timezone: "UTC"

tasks:
  - description: "Cron task"
    cron_expression: "0 22 * * *"

  - description: "One-shot task"
    execute_after: "2025-01-01T00:00:00Z"
"""
        )
        config = scheduler.load_config(path)
        assert len(config.tasks) == 2
        jobs = scheduler.list_jobs()
        assert len(jobs) == 2


# ── Helper functions ───────────────────────────────────────────────

def _write_and_load_yaml(content: str) -> SchedulerConfig:
    """Write YAML content to a temp file and parse it."""
    path = _write_temp_yaml(content)
    return load_scheduler_config(path)


def _write_temp_yaml(content: str) -> str:
    """Write YAML content to a temporary file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    return path
