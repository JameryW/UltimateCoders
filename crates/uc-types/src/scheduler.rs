//! Scheduler types for task scheduling and night-time orchestration.
//!
//! Defines the data types used by the scheduling system:
//! - `ScheduledTask`: A task registered with the scheduler (cron or one-shot)
//! - `ExecutionHistory`: Record of a scheduled task execution
//! - `NightWindowConfig`: Configuration for the night-time execution window
//! - `ExecutionStatus`: Status of a scheduled task execution

use chrono::{DateTime, NaiveTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// A task registered with the scheduler.
///
/// Can be either a recurring cron task (e.g., "rebuild index every night at 22:00")
/// or a one-shot delayed task (e.g., "run this review tonight").
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScheduledTask {
    /// Unique identifier for this scheduled task.
    pub id: Uuid,
    /// Human-readable description of the task.
    pub description: String,
    /// The project/repository context for the task.
    pub project_id: String,
    /// Cron expression for recurring tasks (e.g., "0 22 * * *").
    /// None for one-shot tasks.
    pub cron_expression: Option<String>,
    /// Execute after this timestamp (for one-shot delayed tasks).
    /// None for recurring cron tasks.
    pub execute_after: Option<DateTime<Utc>>,
    /// Start of the night execution window (e.g., 22:00).
    pub night_window_start: NaiveTime,
    /// End of the night execution window (e.g., 06:00).
    pub night_window_end: NaiveTime,
    /// IANA timezone name for the night window (e.g., "Asia/Shanghai").
    pub timezone: String,
    /// Whether this scheduled task is enabled.
    pub enabled: bool,
    /// Timestamp of the last execution.
    pub last_execution: Option<DateTime<Utc>>,
    /// Timestamp of the next scheduled execution.
    pub next_execution: Option<DateTime<Utc>>,
    /// When this scheduled task was created.
    pub created_at: DateTime<Utc>,
    /// When this scheduled task was last updated.
    pub updated_at: DateTime<Utc>,
}

impl ScheduledTask {
    /// Create a new scheduled task with default values.
    pub fn new(
        description: String,
        project_id: String,
        night_window_start: NaiveTime,
        night_window_end: NaiveTime,
        timezone: String,
    ) -> Self {
        let now = Utc::now();
        Self {
            id: Uuid::new_v4(),
            description,
            project_id,
            cron_expression: None,
            execute_after: None,
            night_window_start,
            night_window_end,
            timezone,
            enabled: true,
            last_execution: None,
            next_execution: None,
            created_at: now,
            updated_at: now,
        }
    }

    /// Create a cron-based scheduled task.
    pub fn cron(
        description: String,
        project_id: String,
        cron_expression: String,
        night_window_start: NaiveTime,
        night_window_end: NaiveTime,
        timezone: String,
    ) -> Self {
        let mut task = Self::new(
            description,
            project_id,
            night_window_start,
            night_window_end,
            timezone,
        );
        task.cron_expression = Some(cron_expression);
        task
    }

    /// Create a one-shot delayed task.
    pub fn one_shot(
        description: String,
        project_id: String,
        execute_after: DateTime<Utc>,
        night_window_start: NaiveTime,
        night_window_end: NaiveTime,
        timezone: String,
    ) -> Self {
        let mut task = Self::new(
            description,
            project_id,
            night_window_start,
            night_window_end,
            timezone,
        );
        task.execute_after = Some(execute_after);
        task
    }

    /// Whether this is a recurring cron task.
    pub fn is_cron(&self) -> bool {
        self.cron_expression.is_some()
    }

    /// Whether this is a one-shot delayed task.
    pub fn is_one_shot(&self) -> bool {
        self.execute_after.is_some() && self.cron_expression.is_none()
    }
}

/// Status of a scheduled task execution.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum ExecutionStatus {
    /// Task completed successfully.
    Completed,
    /// Task failed.
    Failed,
    /// Task was skipped (e.g., disabled or outside window).
    Skipped,
    /// Task was deferred to the next night window.
    Deferred,
}

/// Record of a scheduled task execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionHistory {
    /// Unique identifier for this execution record.
    pub id: Uuid,
    /// The scheduled task that was executed.
    pub scheduled_task_id: Uuid,
    /// When the execution started.
    pub started_at: DateTime<Utc>,
    /// When the execution completed (None if still running or skipped).
    pub completed_at: Option<DateTime<Utc>>,
    /// The status of this execution.
    pub status: ExecutionStatus,
    /// Summary of the execution result.
    pub result_summary: Option<String>,
    /// Reason for deferral (if status is Deferred).
    pub deferred_reason: Option<String>,
}

impl ExecutionHistory {
    /// Create a new execution history record with "started" state.
    pub fn started(scheduled_task_id: Uuid) -> Self {
        Self {
            id: Uuid::new_v4(),
            scheduled_task_id,
            started_at: Utc::now(),
            completed_at: None,
            status: ExecutionStatus::Completed,
            result_summary: None,
            deferred_reason: None,
        }
    }

    /// Create a deferred execution history record.
    pub fn deferred(scheduled_task_id: Uuid, reason: String) -> Self {
        let now = Utc::now();
        Self {
            id: Uuid::new_v4(),
            scheduled_task_id,
            started_at: now,
            completed_at: Some(now),
            status: ExecutionStatus::Deferred,
            result_summary: None,
            deferred_reason: Some(reason),
        }
    }

    /// Create a skipped execution history record.
    pub fn skipped(scheduled_task_id: Uuid, reason: String) -> Self {
        let now = Utc::now();
        Self {
            id: Uuid::new_v4(),
            scheduled_task_id,
            started_at: now,
            completed_at: Some(now),
            status: ExecutionStatus::Skipped,
            result_summary: Some(reason),
            deferred_reason: None,
        }
    }
}

/// Configuration for the night-time execution window.
///
/// Defines the time window during which scheduled tasks are allowed
/// to execute. Supports cross-midnight windows (e.g., 22:00-06:00).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NightWindowConfig {
    /// Start of the night window (e.g., 22:00).
    pub start: NaiveTime,
    /// End of the night window (e.g., 06:00).
    pub end: NaiveTime,
    /// IANA timezone name (e.g., "Asia/Shanghai", "UTC").
    pub timezone: String,
}

impl NightWindowConfig {
    /// Create a new night window configuration.
    pub fn new(start: NaiveTime, end: NaiveTime, timezone: String) -> Self {
        Self {
            start,
            end,
            timezone,
        }
    }

    /// Default night window: 22:00-06:00 in UTC.
    pub fn default_utc() -> Self {
        Self {
            start: NaiveTime::from_hms_opt(22, 0, 0).expect("valid time"),
            end: NaiveTime::from_hms_opt(6, 0, 0).expect("valid time"),
            timezone: "UTC".to_string(),
        }
    }

    /// Whether the window crosses midnight (start > end).
    pub fn crosses_midnight(&self) -> bool {
        self.start > self.end
    }
}

impl Default for NightWindowConfig {
    fn default() -> Self {
        Self::default_utc()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scheduled_task_cron_creation() {
        let task = ScheduledTask::cron(
            "Rebuild index".to_string(),
            "project-1".to_string(),
            "0 22 * * *".to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        assert!(task.is_cron());
        assert!(!task.is_one_shot());
        assert!(task.enabled);
        assert_eq!(task.cron_expression, Some("0 22 * * *".to_string()));
    }

    #[test]
    fn scheduled_task_one_shot_creation() {
        let later = Utc::now() + chrono::Duration::hours(8);
        let task = ScheduledTask::one_shot(
            "Run review".to_string(),
            "project-1".to_string(),
            later,
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        assert!(!task.is_cron());
        assert!(task.is_one_shot());
        assert!(task.execute_after.is_some());
    }

    #[test]
    fn night_window_config_crosses_midnight() {
        let config = NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        assert!(config.crosses_midnight());
    }

    #[test]
    fn night_window_config_same_day() {
        let config = NightWindowConfig::new(
            NaiveTime::from_hms_opt(20, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(23, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        assert!(!config.crosses_midnight());
    }

    #[test]
    fn execution_status_serialization() {
        let status = ExecutionStatus::Deferred;
        let json = serde_json::to_string(&status).unwrap();
        assert_eq!(json, "\"Deferred\"");
        let deserialized: ExecutionStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(status, deserialized);
    }

    #[test]
    fn execution_history_deferred() {
        let task_id = Uuid::new_v4();
        let history = ExecutionHistory::deferred(task_id, "Outside night window".to_string());
        assert_eq!(history.status, ExecutionStatus::Deferred);
        assert_eq!(
            history.deferred_reason,
            Some("Outside night window".to_string())
        );
        assert!(history.completed_at.is_some());
    }

    #[test]
    fn execution_history_skipped() {
        let task_id = Uuid::new_v4();
        let history = ExecutionHistory::skipped(task_id, "Task disabled".to_string());
        assert_eq!(history.status, ExecutionStatus::Skipped);
        assert_eq!(history.result_summary, Some("Task disabled".to_string()));
    }
}
