"""Scheduler configuration — YAML config parsing for scheduled tasks.

Defines dataclasses for YAML configuration entries and provides
`load_scheduler_config()` to parse and validate scheduler YAML files.

Example YAML:
    night_window:
      start: "22:00"
      end: "06:00"
      timezone: "Asia/Shanghai"

    tasks:
      - description: "Rebuild search index for project-alpha"
        cron_expression: "0 22 * * *"
        project_id: "project-alpha"

      - description: "Run code review for project-beta"
        cron_expression: "0 23 * * 1-5"
        project_id: "project-beta"

      - description: "Consolidate knowledge base"
        cron_expression: "0 2 * * 0"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ── Dataclasses ────────────────────────────────────────────────────

@dataclass
class NightWindowConfig:
    """Configuration for the night-time execution window."""

    start: str = "22:00"
    """Start of the night window in HH:MM format (e.g., "22:00")."""

    end: str = "06:00"
    """End of the night window in HH:MM format (e.g., "06:00")."""

    timezone: str = "UTC"
    """IANA timezone name (e.g., "Asia/Shanghai", "UTC")."""


@dataclass
class ScheduledTaskConfig:
    """Configuration for a single scheduled task entry from YAML."""

    description: str
    """Human-readable description of the task."""

    cron_expression: Optional[str] = None
    """Cron expression for recurring tasks (e.g., "0 22 * * *")."""

    execute_after: Optional[str] = None
    """ISO 8601 datetime for one-shot delayed tasks (e.g., "2024-01-15T22:00:00Z")."""

    project_id: Optional[str] = None
    """Project/repository context for the task."""

    night_window_start: Optional[str] = None
    """Override night window start time in HH:MM format."""

    night_window_end: Optional[str] = None
    """Override night window end time in HH:MM format."""

    timezone: str = "UTC"
    """IANA timezone name for this task."""

    enabled: bool = True
    """Whether this task is enabled."""


@dataclass
class SchedulerConfig:
    """Top-level scheduler configuration parsed from YAML."""

    night_window: Optional[NightWindowConfig] = None
    """Global night window configuration."""

    tasks: list[ScheduledTaskConfig] = field(default_factory=list)
    """List of scheduled task configurations."""


# ── Validation helpers ─────────────────────────────────────────────

_TIME_RE = re.compile(r"^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$")


def _validate_time_format(value: str, field_name: str) -> None:
    """Validate a time string is in HH:MM format."""
    if not _TIME_RE.match(value):
        raise ValueError(
            f"Invalid {field_name} format '{value}'. Expected HH:MM (e.g., '22:00')"
        )


def _validate_task_config(task: ScheduledTaskConfig) -> None:
    """Validate a single task configuration entry."""
    if not task.description:
        raise ValueError("Task 'description' is required and must not be empty")

    if task.cron_expression and task.execute_after:
        raise ValueError(
            f"Task '{task.description}': cannot specify both cron_expression and execute_after. "
            "Use cron_expression for recurring tasks, execute_after for one-shot tasks."
        )

    if not task.cron_expression and not task.execute_after:
        raise ValueError(
            f"Task '{task.description}': must specify either cron_expression or execute_after"
        )

    if task.night_window_start:
        _validate_time_format(task.night_window_start, "night_window_start")
    if task.night_window_end:
        _validate_time_format(task.night_window_end, "night_window_end")


def _parse_task_entry(data: dict[str, Any]) -> ScheduledTaskConfig:
    """Parse a single task entry from YAML dict."""
    return ScheduledTaskConfig(
        description=data.get("description", ""),
        cron_expression=data.get("cron_expression"),
        execute_after=data.get("execute_after"),
        project_id=data.get("project_id"),
        night_window_start=data.get("night_window_start"),
        night_window_end=data.get("night_window_end"),
        timezone=data.get("timezone", "UTC"),
        enabled=data.get("enabled", True),
    )


def _parse_night_window(data: dict[str, Any]) -> NightWindowConfig:
    """Parse night window configuration from YAML dict."""
    nw = NightWindowConfig(
        start=data.get("start", "22:00"),
        end=data.get("end", "06:00"),
        timezone=data.get("timezone", "UTC"),
    )
    _validate_time_format(nw.start, "night_window.start")
    _validate_time_format(nw.end, "night_window.end")
    return nw


# ── Public API ─────────────────────────────────────────────────────

def load_scheduler_config(path: str) -> SchedulerConfig:
    """Parse a YAML configuration file and return a SchedulerConfig.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        SchedulerConfig with validated night_window and tasks.

    Raises:
        ImportError: If PyYAML is not installed.
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or validation fails.
    """
    if yaml is None:
        raise ImportError(
            "PyYAML is required for scheduler config loading. "
            "Install it with: pip install pyyaml"
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"YAML file '{path}' is empty")

    config = SchedulerConfig()

    # Parse night_window section
    if "night_window" in raw and raw["night_window"]:
        config.night_window = _parse_night_window(raw["night_window"])

    # Parse tasks section
    tasks_data = raw.get("tasks", [])
    if not isinstance(tasks_data, list):
        raise ValueError(f"YAML 'tasks' must be a list, got {type(tasks_data).__name__}")

    for i, task_data in enumerate(tasks_data):
        if not isinstance(task_data, dict):
            raise ValueError(
                f"Task at index {i} must be a dict, got {type(task_data).__name__}"
            )
        task = _parse_task_entry(task_data)
        _validate_task_config(task)
        config.tasks.append(task)

    return config
