//! Schedule persistence store trait and implementations.
//!
//! Provides the `ScheduleStore` trait for abstracting schedule persistence,
//! with two implementations:
//! - `InMemoryScheduleStore`: In-memory store for testing (always available)
//! - `PostgresScheduleStore`: PostgreSQL-backed store for production (requires `storage` feature)

use async_trait::async_trait;
use uc_types::{EngineError, ExecutionHistory, ScheduledTask};
use uuid::Uuid;

/// Trait for schedule persistence.
///
/// Abstracts over PostgreSQL (production) and in-memory (testing) backends.
#[async_trait]
pub trait ScheduleStore: Send + Sync {
    /// Save a new scheduled task.
    async fn save_task(&self, task: &ScheduledTask) -> Result<(), EngineError>;

    /// Load a scheduled task by ID.
    async fn load_task(&self, id: &Uuid) -> Result<Option<ScheduledTask>, EngineError>;

    /// List all scheduled tasks, optionally filtering to enabled only.
    async fn list_tasks(&self, enabled_only: bool) -> Result<Vec<ScheduledTask>, EngineError>;

    /// Update an existing scheduled task.
    async fn update_task(&self, task: &ScheduledTask) -> Result<(), EngineError>;

    /// Delete a scheduled task by ID.
    async fn delete_task(&self, id: &Uuid) -> Result<(), EngineError>;

    /// Save an execution history record.
    async fn save_execution(&self, history: &ExecutionHistory) -> Result<(), EngineError>;

    /// List execution history for a task, limited to the most recent entries.
    async fn list_executions(
        &self,
        task_id: &Uuid,
        limit: i64,
    ) -> Result<Vec<ExecutionHistory>, EngineError>;
}

// ── In-memory implementation ─────────────────────────────────────

/// In-memory schedule store for testing.
pub struct InMemoryScheduleStore {
    tasks: tokio::sync::RwLock<Vec<ScheduledTask>>,
    executions: tokio::sync::RwLock<Vec<ExecutionHistory>>,
}

impl InMemoryScheduleStore {
    /// Create a new empty in-memory store.
    pub fn new() -> Self {
        Self {
            tasks: tokio::sync::RwLock::new(Vec::new()),
            executions: tokio::sync::RwLock::new(Vec::new()),
        }
    }
}

impl Default for InMemoryScheduleStore {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl ScheduleStore for InMemoryScheduleStore {
    async fn save_task(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        let mut tasks = self.tasks.write().await;
        // Check for duplicate ID
        if tasks.iter().any(|t| t.id == task.id) {
            return Err(EngineError::TaskError(format!(
                "Scheduled task {} already exists",
                task.id
            )));
        }
        tasks.push(task.clone());
        Ok(())
    }

    async fn load_task(&self, id: &Uuid) -> Result<Option<ScheduledTask>, EngineError> {
        let tasks = self.tasks.read().await;
        Ok(tasks.iter().find(|t| t.id == *id).cloned())
    }

    async fn list_tasks(&self, enabled_only: bool) -> Result<Vec<ScheduledTask>, EngineError> {
        let tasks = self.tasks.read().await;
        Ok(tasks
            .iter()
            .filter(|t| !enabled_only || t.enabled)
            .cloned()
            .collect())
    }

    async fn update_task(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        let mut tasks = self.tasks.write().await;
        if let Some(existing) = tasks.iter_mut().find(|t| t.id == task.id) {
            *existing = task.clone();
            Ok(())
        } else {
            Err(EngineError::TaskError(format!(
                "Scheduled task {} not found for update",
                task.id
            )))
        }
    }

    async fn delete_task(&self, id: &Uuid) -> Result<(), EngineError> {
        let mut tasks = self.tasks.write().await;
        let before = tasks.len();
        tasks.retain(|t| t.id != *id);
        if tasks.len() == before {
            Err(EngineError::TaskError(format!(
                "Scheduled task {} not found for deletion",
                id
            )))
        } else {
            // Also remove associated execution history
            let mut executions = self.executions.write().await;
            executions.retain(|e| e.scheduled_task_id != *id);
            Ok(())
        }
    }

    async fn save_execution(&self, history: &ExecutionHistory) -> Result<(), EngineError> {
        let mut executions = self.executions.write().await;
        executions.push(history.clone());
        Ok(())
    }

    async fn list_executions(
        &self,
        task_id: &Uuid,
        limit: i64,
    ) -> Result<Vec<ExecutionHistory>, EngineError> {
        let executions = self.executions.read().await;
        Ok(executions
            .iter()
            .filter(|e| e.scheduled_task_id == *task_id)
            .rev()
            .take(limit as usize)
            .cloned()
            .collect())
    }
}

// ── PostgreSQL implementation ────────────────────────────────────

#[cfg(feature = "storage")]
mod postgres {
    use super::*;
    use sqlx::postgres::PgPool;
    use std::sync::Arc;

    /// PostgreSQL-backed schedule store.
    pub struct PostgresScheduleStore {
        pool: Arc<PgPool>,
    }

    impl PostgresScheduleStore {
        /// Create a new PostgreSQL schedule store with a connection pool.
        pub fn new(pool: Arc<PgPool>) -> Self {
            Self { pool }
        }
    }

    #[async_trait]
    impl ScheduleStore for PostgresScheduleStore {
        async fn save_task(&self, task: &ScheduledTask) -> Result<(), EngineError> {
            sqlx::query(
                r#"
                INSERT INTO scheduled_tasks (
                    id, description, project_id, cron_expression, execute_after,
                    night_window_start, night_window_end, timezone, enabled,
                    last_execution, next_execution, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                "#,
            )
            .bind(task.id)
            .bind(&task.description)
            .bind(&task.project_id)
            .bind(&task.cron_expression)
            .bind(task.execute_after)
            .bind(task.night_window_start)
            .bind(task.night_window_end)
            .bind(&task.timezone)
            .bind(task.enabled)
            .bind(task.last_execution)
            .bind(task.next_execution)
            .bind(task.created_at)
            .bind(task.updated_at)
            .execute(self.pool.as_ref())
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to save scheduled task: {}", e))
            })?;
            Ok(())
        }

        async fn load_task(&self, id: &Uuid) -> Result<Option<ScheduledTask>, EngineError> {
            let row = sqlx::query_as::<
                _,
                (
                    Uuid,
                    String,
                    String,
                    Option<String>,
                    Option<chrono::DateTime<chrono::Utc>>,
                    chrono::NaiveTime,
                    chrono::NaiveTime,
                    String,
                    bool,
                    Option<chrono::DateTime<chrono::Utc>>,
                    Option<chrono::DateTime<chrono::Utc>>,
                    chrono::DateTime<chrono::Utc>,
                    chrono::DateTime<chrono::Utc>,
                ),
            >(
                r#"
                SELECT id, description, project_id, cron_expression, execute_after,
                       night_window_start, night_window_end, timezone, enabled,
                       last_execution, next_execution, created_at, updated_at
                FROM scheduled_tasks WHERE id = $1
                "#,
            )
            .bind(id)
            .fetch_optional(self.pool.as_ref())
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to load scheduled task: {}", e))
            })?;

            Ok(row.map(|r| ScheduledTask {
                id: r.0,
                description: r.1,
                project_id: r.2,
                cron_expression: r.3,
                execute_after: r.4,
                night_window_start: r.5,
                night_window_end: r.6,
                timezone: r.7,
                enabled: r.8,
                last_execution: r.9,
                next_execution: r.10,
                created_at: r.11,
                updated_at: r.12,
            }))
        }

        async fn list_tasks(&self, enabled_only: bool) -> Result<Vec<ScheduledTask>, EngineError> {
            let rows = if enabled_only {
                sqlx::query_as::<
                    _,
                    (
                        Uuid,
                        String,
                        String,
                        Option<String>,
                        Option<chrono::DateTime<chrono::Utc>>,
                        chrono::NaiveTime,
                        chrono::NaiveTime,
                        String,
                        bool,
                        Option<chrono::DateTime<chrono::Utc>>,
                        Option<chrono::DateTime<chrono::Utc>>,
                        chrono::DateTime<chrono::Utc>,
                        chrono::DateTime<chrono::Utc>,
                    ),
                >(
                    r#"
                    SELECT id, description, project_id, cron_expression, execute_after,
                           night_window_start, night_window_end, timezone, enabled,
                           last_execution, next_execution, created_at, updated_at
                    FROM scheduled_tasks WHERE enabled = TRUE ORDER BY created_at
                    "#,
                )
                .fetch_all(self.pool.as_ref())
                .await
            } else {
                sqlx::query_as::<
                    _,
                    (
                        Uuid,
                        String,
                        String,
                        Option<String>,
                        Option<chrono::DateTime<chrono::Utc>>,
                        chrono::NaiveTime,
                        chrono::NaiveTime,
                        String,
                        bool,
                        Option<chrono::DateTime<chrono::Utc>>,
                        Option<chrono::DateTime<chrono::Utc>>,
                        chrono::DateTime<chrono::Utc>,
                        chrono::DateTime<chrono::Utc>,
                    ),
                >(
                    r#"
                    SELECT id, description, project_id, cron_expression, execute_after,
                           night_window_start, night_window_end, timezone, enabled,
                           last_execution, next_execution, created_at, updated_at
                    FROM scheduled_tasks ORDER BY created_at
                    "#,
                )
                .fetch_all(self.pool.as_ref())
                .await
            }
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to list scheduled tasks: {}", e))
            })?;

            Ok(rows
                .into_iter()
                .map(|r| ScheduledTask {
                    id: r.0,
                    description: r.1,
                    project_id: r.2,
                    cron_expression: r.3,
                    execute_after: r.4,
                    night_window_start: r.5,
                    night_window_end: r.6,
                    timezone: r.7,
                    enabled: r.8,
                    last_execution: r.9,
                    next_execution: r.10,
                    created_at: r.11,
                    updated_at: r.12,
                })
                .collect())
        }

        async fn update_task(&self, task: &ScheduledTask) -> Result<(), EngineError> {
            let result = sqlx::query(
                r#"
                UPDATE scheduled_tasks SET
                    description = $2, project_id = $3, cron_expression = $4,
                    execute_after = $5, night_window_start = $6, night_window_end = $7,
                    timezone = $8, enabled = $9, last_execution = $10,
                    next_execution = $11, updated_at = $12
                WHERE id = $1
                "#,
            )
            .bind(task.id)
            .bind(&task.description)
            .bind(&task.project_id)
            .bind(&task.cron_expression)
            .bind(task.execute_after)
            .bind(task.night_window_start)
            .bind(task.night_window_end)
            .bind(&task.timezone)
            .bind(task.enabled)
            .bind(task.last_execution)
            .bind(task.next_execution)
            .bind(task.updated_at)
            .execute(self.pool.as_ref())
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to update scheduled task: {}", e))
            })?;

            if result.rows_affected() == 0 {
                return Err(EngineError::TaskError(format!(
                    "Scheduled task {} not found for update",
                    task.id
                )));
            }
            Ok(())
        }

        async fn delete_task(&self, id: &Uuid) -> Result<(), EngineError> {
            // Execution history will be cascade-deleted via FK constraint
            let result = sqlx::query("DELETE FROM scheduled_tasks WHERE id = $1")
                .bind(id)
                .execute(self.pool.as_ref())
                .await
                .map_err(|e| {
                    EngineError::ConnectionError(format!("Failed to delete scheduled task: {}", e))
                })?;

            if result.rows_affected() == 0 {
                return Err(EngineError::TaskError(format!(
                    "Scheduled task {} not found for deletion",
                    id
                )));
            }
            Ok(())
        }

        async fn save_execution(&self, history: &ExecutionHistory) -> Result<(), EngineError> {
            sqlx::query(
                r#"
                INSERT INTO execution_history (
                    id, scheduled_task_id, started_at, completed_at,
                    status, result_summary, deferred_reason, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                "#,
            )
            .bind(history.id)
            .bind(history.scheduled_task_id)
            .bind(history.started_at)
            .bind(history.completed_at)
            .bind(execution_status_to_str(&history.status))
            .bind(&history.result_summary)
            .bind(&history.deferred_reason)
            .bind(chrono::Utc::now())
            .execute(self.pool.as_ref())
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to save execution history: {}", e))
            })?;
            Ok(())
        }

        async fn list_executions(
            &self,
            task_id: &Uuid,
            limit: i64,
        ) -> Result<Vec<ExecutionHistory>, EngineError> {
            let rows = sqlx::query_as::<
                _,
                (
                    Uuid,
                    Uuid,
                    chrono::DateTime<chrono::Utc>,
                    Option<chrono::DateTime<chrono::Utc>>,
                    String,
                    Option<String>,
                    Option<String>,
                ),
            >(
                r#"
                SELECT id, scheduled_task_id, started_at, completed_at,
                       status, result_summary, deferred_reason
                FROM execution_history
                WHERE scheduled_task_id = $1
                ORDER BY started_at DESC
                LIMIT $2
                "#,
            )
            .bind(task_id)
            .bind(limit)
            .fetch_all(self.pool.as_ref())
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("Failed to list execution history: {}", e))
            })?;

            Ok(rows
                .into_iter()
                .map(|r| ExecutionHistory {
                    id: r.0,
                    scheduled_task_id: r.1,
                    started_at: r.2,
                    completed_at: r.3,
                    status: parse_execution_status(&r.4),
                    result_summary: r.5,
                    deferred_reason: r.6,
                })
                .collect())
        }
    }

    fn execution_status_to_str(status: &uc_types::ExecutionStatus) -> &'static str {
        super::execution_status_to_str(status)
    }

    fn parse_execution_status(s: &str) -> uc_types::ExecutionStatus {
        super::parse_execution_status(s)
    }
}

#[cfg(feature = "storage")]
pub use postgres::PostgresScheduleStore;

// ── Helper functions for ExecutionStatus ↔ String conversion ─────

/// Convert an ExecutionStatus to its string representation.
pub fn execution_status_to_str(status: &uc_types::ExecutionStatus) -> &'static str {
    match status {
        uc_types::ExecutionStatus::Completed => "Completed",
        uc_types::ExecutionStatus::Failed => "Failed",
        uc_types::ExecutionStatus::Skipped => "Skipped",
        uc_types::ExecutionStatus::Deferred => "Deferred",
    }
}

/// Parse an ExecutionStatus from its string representation.
pub fn parse_execution_status(s: &str) -> uc_types::ExecutionStatus {
    match s {
        "Completed" => uc_types::ExecutionStatus::Completed,
        "Failed" => uc_types::ExecutionStatus::Failed,
        "Skipped" => uc_types::ExecutionStatus::Skipped,
        "Deferred" => uc_types::ExecutionStatus::Deferred,
        _ => uc_types::ExecutionStatus::Failed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{NaiveTime, Utc};

    fn make_cron_task(description: &str, cron: &str) -> ScheduledTask {
        ScheduledTask::cron(
            description.to_string(),
            "test-project".to_string(),
            cron.to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    fn make_one_shot_task(description: &str) -> ScheduledTask {
        ScheduledTask::one_shot(
            description.to_string(),
            "test-project".to_string(),
            Utc::now() + chrono::Duration::hours(8),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    #[tokio::test]
    async fn in_memory_save_and_load_task() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Rebuild index", "0 22 * * *");

        store.save_task(&task).await.unwrap();
        let loaded = store.load_task(&task.id).await.unwrap();
        assert!(loaded.is_some());
        assert_eq!(loaded.unwrap().description, "Rebuild index");
    }

    #[tokio::test]
    async fn in_memory_load_nonexistent_task() {
        let store = InMemoryScheduleStore::new();
        let loaded = store.load_task(&Uuid::new_v4()).await.unwrap();
        assert!(loaded.is_none());
    }

    #[tokio::test]
    async fn in_memory_save_duplicate_task_fails() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Rebuild index", "0 22 * * *");

        store.save_task(&task).await.unwrap();
        let result = store.save_task(&task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn in_memory_list_tasks() {
        let store = InMemoryScheduleStore::new();
        let task1 = make_cron_task("Task 1", "0 22 * * *");
        let task2 = make_cron_task("Task 2", "0 23 * * *");

        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        let all = store.list_tasks(false).await.unwrap();
        assert_eq!(all.len(), 2);

        let enabled = store.list_tasks(true).await.unwrap();
        assert_eq!(enabled.len(), 2);
    }

    #[tokio::test]
    async fn in_memory_list_tasks_enabled_only() {
        let store = InMemoryScheduleStore::new();
        let mut task1 = make_cron_task("Task 1", "0 22 * * *");
        let task2 = make_cron_task("Task 2", "0 23 * * *");

        task1.enabled = false;

        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        let all = store.list_tasks(false).await.unwrap();
        assert_eq!(all.len(), 2);

        let enabled = store.list_tasks(true).await.unwrap();
        assert_eq!(enabled.len(), 1);
        assert_eq!(enabled[0].description, "Task 2");
    }

    #[tokio::test]
    async fn in_memory_update_task() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Original", "0 22 * * *");

        store.save_task(&task).await.unwrap();

        let mut updated = task.clone();
        updated.description = "Updated".to_string();
        store.update_task(&updated).await.unwrap();

        let loaded = store.load_task(&task.id).await.unwrap().unwrap();
        assert_eq!(loaded.description, "Updated");
    }

    #[tokio::test]
    async fn in_memory_update_nonexistent_task_fails() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Nonexistent", "0 22 * * *");
        let result = store.update_task(&task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn in_memory_delete_task() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("To delete", "0 22 * * *");

        store.save_task(&task).await.unwrap();
        store.delete_task(&task.id).await.unwrap();

        let loaded = store.load_task(&task.id).await.unwrap();
        assert!(loaded.is_none());
    }

    #[tokio::test]
    async fn in_memory_delete_nonexistent_task_fails() {
        let store = InMemoryScheduleStore::new();
        let result = store.delete_task(&Uuid::new_v4()).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn in_memory_delete_task_removes_executions() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Task with history", "0 22 * * *");

        store.save_task(&task).await.unwrap();

        let history = uc_types::ExecutionHistory::started(task.id);
        store.save_execution(&history).await.unwrap();

        // Verify execution was saved
        let executions = store.list_executions(&task.id, 10).await.unwrap();
        assert_eq!(executions.len(), 1);

        // Delete the task
        store.delete_task(&task.id).await.unwrap();

        // Executions should be removed too
        let executions = store.list_executions(&task.id, 10).await.unwrap();
        assert!(executions.is_empty());
    }

    #[tokio::test]
    async fn in_memory_save_and_list_executions() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Task", "0 22 * * *");

        store.save_task(&task).await.unwrap();

        let h1 = uc_types::ExecutionHistory::started(task.id);
        let h2 = uc_types::ExecutionHistory::deferred(task.id, "Outside window".to_string());

        store.save_execution(&h1).await.unwrap();
        store.save_execution(&h2).await.unwrap();

        let executions = store.list_executions(&task.id, 10).await.unwrap();
        assert_eq!(executions.len(), 2);
    }

    #[tokio::test]
    async fn in_memory_list_executions_with_limit() {
        let store = InMemoryScheduleStore::new();
        let task = make_cron_task("Task", "0 22 * * *");

        store.save_task(&task).await.unwrap();

        for _ in 0..5 {
            let h = uc_types::ExecutionHistory::started(task.id);
            store.save_execution(&h).await.unwrap();
        }

        let executions = store.list_executions(&task.id, 3).await.unwrap();
        assert_eq!(executions.len(), 3);
    }

    #[tokio::test]
    async fn in_memory_list_executions_filters_by_task() {
        let store = InMemoryScheduleStore::new();
        let task1 = make_cron_task("Task 1", "0 22 * * *");
        let task2 = make_cron_task("Task 2", "0 23 * * *");

        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        let h1 = uc_types::ExecutionHistory::started(task1.id);
        let h2 = uc_types::ExecutionHistory::started(task2.id);

        store.save_execution(&h1).await.unwrap();
        store.save_execution(&h2).await.unwrap();

        let executions1 = store.list_executions(&task1.id, 10).await.unwrap();
        assert_eq!(executions1.len(), 1);

        let executions2 = store.list_executions(&task2.id, 10).await.unwrap();
        assert_eq!(executions2.len(), 1);
    }

    #[tokio::test]
    async fn in_memory_one_shot_task_crud() {
        let store = InMemoryScheduleStore::new();
        let task = make_one_shot_task("Run review");

        store.save_task(&task).await.unwrap();
        let loaded = store.load_task(&task.id).await.unwrap().unwrap();
        assert!(loaded.is_one_shot());

        store.delete_task(&task.id).await.unwrap();
        let loaded = store.load_task(&task.id).await.unwrap();
        assert!(loaded.is_none());
    }

    #[test]
    fn execution_status_roundtrip() {
        let statuses = [
            uc_types::ExecutionStatus::Completed,
            uc_types::ExecutionStatus::Failed,
            uc_types::ExecutionStatus::Skipped,
            uc_types::ExecutionStatus::Deferred,
        ];
        for status in &statuses {
            let s = execution_status_to_str(status);
            let parsed = parse_execution_status(s);
            assert_eq!(*status, parsed, "Failed roundtrip for {:?}", status);
        }
    }
}
