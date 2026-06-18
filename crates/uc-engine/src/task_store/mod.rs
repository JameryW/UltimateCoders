//! Task store backend trait and implementations.
//!
//! Provides a swappable persistence layer for task state:
//! - `TaskStoreBackend` trait — async contract for task CRUD + heartbeat
//! - `InMemoryTaskBackend` — HashMap-backed, always available
//! - `PostgresTaskBackend` — sqlx-backed, feature-gated on `storage`
//!
//! The trait follows the same dual-path pattern as `PostgresMetadataStore`:
//! when PostgreSQL is unavailable, the in-memory fallback is used instead.
//!
//! Legacy: the synchronous `TaskStore` struct (used by `LocalEngine`) is
//! re-exported from the `legacy` submodule for backward compatibility.

// Legacy synchronous TaskStore for LocalEngine backward compat.
mod legacy;
pub use legacy::TaskStore;

use std::sync::Arc;

use uc_types::{EngineError, Task, TaskId, TaskStatus};

// ── Task update type (shared between NATS and local worker) ─────

/// A task update payload that can be applied to the store.
///
/// This is a backend-agnostic representation that both the NATS subscriber
/// and the local worker bridge can produce.
#[derive(Debug, Clone)]
pub struct TaskUpdate {
    pub task_id: String,
    pub status: String,
    pub description: String,
    pub project_id: String,
    pub subtasks: Vec<SubtaskUpdate>,
    pub result: Option<String>,
}

/// A subtask update within a `TaskUpdate`.
#[derive(Debug, Clone)]
pub struct SubtaskUpdate {
    pub subtask_id: String,
    pub status: String,
    pub description: String,
    pub assigned_worker: Option<String>,
    pub depends_on: Vec<String>,
}

// ── TaskStoreBackend trait ────────────────────────────────────

/// Async trait for task store backends.
///
/// Implementations:
/// - `InMemoryTaskBackend`: always available, for testing and fallback
/// - `PostgresTaskBackend`: feature-gated on `storage`, uses sqlx
#[async_trait::async_trait]
pub trait TaskStoreBackend: Send + Sync {
    /// Submit a new task with local decomposition (InProgress status).
    async fn submit_task(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError>;

    /// Submit a new task in Planning status (awaiting external decomposition).
    async fn submit_task_pending(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError>;

    /// Get a task by ID.
    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError>;

    /// List all tasks.
    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError>;

    /// Pause a task. Only InProgress/Planning tasks can be paused.
    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// Resume a task. Only Paused tasks can be resumed.
    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// Apply a status update (from NATS or local worker).
    async fn apply_update(&self, update: &TaskUpdate) -> Result<(), EngineError>;

    /// Set the status of a task by ID. Returns the previous status.
    async fn set_task_status(
        &self,
        task_id: &str,
        new_status: TaskStatus,
    ) -> Result<Option<TaskStatus>, EngineError>;

    /// Update the last heartbeat timestamp.
    async fn update_last_heartbeat(&self) -> Result<(), EngineError>;

    /// Get the last heartbeat timestamp.
    async fn last_heartbeat(&self) -> Result<Option<chrono::DateTime<chrono::Utc>>, EngineError>;

    /// Mark tasks as Failed if no heartbeat within the timeout.
    /// Returns the IDs of tasks that were marked as Failed.
    async fn mark_stale_tasks_failed(
        &self,
        timeout: std::time::Duration,
    ) -> Result<Vec<String>, EngineError>;

    /// Remove a task by ID (used for cleanup on failed NATS publish).
    async fn remove_task(&self, task_id: &str) -> Result<(), EngineError>;

    /// Check if the backend is connected to persistent storage.
    /// Returns false for in-memory fallback.
    fn is_connected(&self) -> bool;
}

// ── InMemoryTaskBackend ──────────────────────────────────────

/// In-memory task store backend using HashMap.
///
/// Always available, used for testing and as a fallback when
/// PostgreSQL is unavailable.
pub struct InMemoryTaskBackend {
    inner: Arc<tokio::sync::Mutex<InMemoryTaskBackendInner>>,
}

/// Inner data protected by Mutex.
struct InMemoryTaskBackendInner {
    tasks: HashMap<String, Task>,
    last_heartbeat: Option<chrono::DateTime<chrono::Utc>>,
}

use std::collections::HashMap;

impl Default for InMemoryTaskBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl InMemoryTaskBackend {
    /// Create a new empty in-memory task backend.
    pub fn new() -> Self {
        Self {
            inner: Arc::new(tokio::sync::Mutex::new(InMemoryTaskBackendInner {
                tasks: HashMap::new(),
                last_heartbeat: None,
            })),
        }
    }
}

#[async_trait::async_trait]
impl TaskStoreBackend for InMemoryTaskBackend {
    async fn submit_task(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError> {
        let mut inner = self.inner.lock().await;
        let task_id = TaskId::new();
        let now = chrono::Utc::now();

        let subtasks = decompose_task(&task_id, &description);

        let task = Task {
            id: task_id.clone(),
            description,
            project_id,
            status: TaskStatus::InProgress,
            subtasks,
            created_at: now,
            updated_at: now,
        };

        inner.tasks.insert(task_id.0.clone(), task.clone());
        Ok(task)
    }

    async fn submit_task_pending(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError> {
        let mut inner = self.inner.lock().await;
        let task_id = TaskId::new();
        let now = chrono::Utc::now();

        let task = Task {
            id: task_id.clone(),
            description,
            project_id,
            status: TaskStatus::Planning,
            subtasks: Vec::new(),
            created_at: now,
            updated_at: now,
        };

        inner.tasks.insert(task_id.0.clone(), task.clone());
        Ok(task)
    }

    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError> {
        let inner = self.inner.lock().await;
        Ok(inner.tasks.get(task_id).cloned())
    }

    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        let inner = self.inner.lock().await;
        Ok(inner.tasks.values().cloned().collect())
    }

    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut inner = self.inner.lock().await;
        let task = inner
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| EngineError::TaskError(format!("Task not found: {}", task_id)))?;
        match &task.status {
            TaskStatus::InProgress | TaskStatus::Planning => {
                task.status = TaskStatus::Paused;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(EngineError::TaskError(format!(
                "Cannot pause task in {:?} status (expected InProgress or Planning)",
                other
            ))),
        }
    }

    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut inner = self.inner.lock().await;
        let task = inner
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| EngineError::TaskError(format!("Task not found: {}", task_id)))?;
        match &task.status {
            TaskStatus::Paused => {
                task.status = TaskStatus::InProgress;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(EngineError::TaskError(format!(
                "Cannot resume task in {:?} status (expected Paused)",
                other
            ))),
        }
    }

    async fn apply_update(&self, update: &TaskUpdate) -> Result<(), EngineError> {
        let mut inner = self.inner.lock().await;
        let task = match inner.tasks.get_mut(&update.task_id) {
            Some(t) => t,
            None => {
                tracing::warn!(
                    task_id = %update.task_id,
                    "Received update for unknown task, ignoring"
                );
                return Ok(());
            }
        };

        // Update task status
        if let Some(status) = task_status_from_str(&update.status) {
            task.status = status;
        }

        // Update subtask statuses
        for subtask_update in &update.subtasks {
            if let Some(subtask) = task
                .subtasks
                .iter_mut()
                .find(|st| st.id.0 == subtask_update.subtask_id)
            {
                if let Some(status) = subtask_status_from_str(&subtask_update.status) {
                    subtask.status = status;
                }
                if let Some(worker) = &subtask_update.assigned_worker {
                    subtask.assigned_worker = Some(uc_types::WorkerId(worker.clone()));
                }
            } else {
                // New subtask from Python Orchestrator — add it
                let new_subtask = uc_types::Subtask {
                    id: TaskId(subtask_update.subtask_id.clone()),
                    parent_id: task.id.clone(),
                    description: subtask_update.description.clone(),
                    status: subtask_status_from_str(&subtask_update.status)
                        .unwrap_or(uc_types::SubtaskStatus::Pending),
                    assigned_worker: subtask_update
                        .assigned_worker
                        .as_ref()
                        .map(|w| uc_types::WorkerId(w.clone())),
                    depends_on: subtask_update
                        .depends_on
                        .iter()
                        .map(|d| TaskId(d.clone()))
                        .collect(),
                    file_constraints: Vec::new(),
                    expected_output: String::new(),
                    result: None,
                };
                task.subtasks.push(new_subtask);
            }
        }

        task.updated_at = chrono::Utc::now();
        Ok(())
    }

    async fn set_task_status(
        &self,
        task_id: &str,
        new_status: TaskStatus,
    ) -> Result<Option<TaskStatus>, EngineError> {
        let mut inner = self.inner.lock().await;
        let task = match inner.tasks.get_mut(task_id) {
            Some(t) => t,
            None => return Ok(None),
        };
        let old = task.status.clone();
        task.status = new_status;
        task.updated_at = chrono::Utc::now();
        Ok(Some(old))
    }

    async fn update_last_heartbeat(&self) -> Result<(), EngineError> {
        let mut inner = self.inner.lock().await;
        inner.last_heartbeat = Some(chrono::Utc::now());
        Ok(())
    }

    async fn last_heartbeat(&self) -> Result<Option<chrono::DateTime<chrono::Utc>>, EngineError> {
        let inner = self.inner.lock().await;
        Ok(inner.last_heartbeat)
    }

    async fn mark_stale_tasks_failed(
        &self,
        timeout: std::time::Duration,
    ) -> Result<Vec<String>, EngineError> {
        let mut inner = self.inner.lock().await;
        let last_hb = match inner.last_heartbeat {
            Some(ts) => ts,
            None => return Ok(Vec::new()),
        };

        let now = chrono::Utc::now();
        let elapsed = now.signed_duration_since(last_hb);
        if elapsed.num_milliseconds() < timeout.as_millis() as i64 {
            return Ok(Vec::new());
        }

        let mut failed_ids = Vec::new();
        for (id, task) in &mut inner.tasks {
            match task.status {
                TaskStatus::InProgress | TaskStatus::Planning => {
                    task.status = TaskStatus::Failed;
                    task.updated_at = now;
                    failed_ids.push(id.clone());
                }
                _ => {}
            }
        }

        if !failed_ids.is_empty() {
            tracing::warn!(
                elapsed_secs = elapsed.num_seconds(),
                tasks_failed = failed_ids.len(),
                "Marked tasks as Failed due to consumer heartbeat timeout"
            );
        }

        Ok(failed_ids)
    }

    async fn remove_task(&self, task_id: &str) -> Result<(), EngineError> {
        let mut inner = self.inner.lock().await;
        inner.tasks.remove(task_id);
        Ok(())
    }

    fn is_connected(&self) -> bool {
        false
    }
}

// ── PostgresTaskBackend (feature-gated) ──────────────────────

/// PostgreSQL-backed task store.
///
/// Feature-gated on `storage`. When PostgreSQL is unavailable,
/// falls back to in-memory storage via the dual-path pattern.
#[cfg(feature = "storage")]
pub struct PostgresTaskBackend {
    pool: Option<Arc<sqlx::postgres::PgPool>>,
    fallback: InMemoryTaskBackend,
}

#[cfg(feature = "storage")]
impl PostgresTaskBackend {
    /// Create a new PostgreSQL task store, connecting to the database.
    pub async fn new(database_url: &str) -> Result<Self, EngineError> {
        match sqlx::postgres::PgPoolOptions::new()
            .max_connections(5)
            .acquire_timeout(std::time::Duration::from_secs(10))
            .connect(database_url)
            .await
        {
            Ok(pool) => {
                let store = Self {
                    pool: Some(Arc::new(pool)),
                    fallback: InMemoryTaskBackend::new(),
                };
                store.run_migrations().await?;
                tracing::info!("Connected to PostgreSQL for task storage");
                Ok(store)
            }
            Err(e) => {
                tracing::warn!(
                    "PostgreSQL unavailable for task store, using in-memory fallback: {}",
                    e
                );
                Ok(Self {
                    pool: None,
                    fallback: InMemoryTaskBackend::new(),
                })
            }
        }
    }

    /// Create with in-memory fallback only (for testing).
    pub fn new_fallback() -> Self {
        Self {
            pool: None,
            fallback: InMemoryTaskBackend::new(),
        }
    }

    /// Create with an existing connection pool (for testing / dependency injection).
    pub fn with_pool(pool: Arc<sqlx::postgres::PgPool>) -> Self {
        Self {
            pool: Some(pool),
            fallback: InMemoryTaskBackend::new(),
        }
    }

    /// Run database migrations to create required tables.
    pub async fn run_migrations(&self) -> Result<(), EngineError> {
        let pool = self
            .pool
            .as_ref()
            .ok_or_else(|| EngineError::ConnectionError("PostgreSQL pool not available".into()))?;

        // Create tasks table
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                subtasks JSONB NOT NULL DEFAULT '[]',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            "#,
        )
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Migration error (tasks): {}", e)))?;

        // Create indexes
        let indexes = [
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)",
        ];

        for idx_sql in &indexes {
            sqlx::query(idx_sql)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Index creation error: {}", e)))?;
        }

        tracing::info!("PostgreSQL task store migrations completed");
        Ok(())
    }

    /// Upsert a task into PostgreSQL.
    async fn upsert_task_pg(
        pool: &Arc<sqlx::postgres::PgPool>,
        task: &Task,
    ) -> Result<(), EngineError> {
        let status_str = task_status_to_str(&task.status);
        let subtasks_json = serde_json::to_value(&task.subtasks)
            .map_err(|e| EngineError::InternalError(format!("Subtask serialization: {}", e)))?;

        sqlx::query(
            r#"
            INSERT INTO tasks (id, description, project_id, status, subtasks, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                description = EXCLUDED.description,
                project_id = EXCLUDED.project_id,
                status = EXCLUDED.status,
                subtasks = EXCLUDED.subtasks,
                updated_at = EXCLUDED.updated_at
            "#,
        )
        .bind(&task.id.0)
        .bind(&task.description)
        .bind(&task.project_id)
        .bind(status_str)
        .bind(&subtasks_json)
        .bind(task.created_at)
        .bind(task.updated_at)
        .execute(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Task upsert error: {}", e)))?;

        Ok(())
    }

    /// Read a task from PostgreSQL.
    async fn read_task_pg(
        pool: &Arc<sqlx::postgres::PgPool>,
        task_id: &str,
    ) -> Result<Option<Task>, EngineError> {
        let row = sqlx::query_as::<_, (String, String, String, String, serde_json::Value, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)>(
            "SELECT id, description, project_id, status, subtasks, created_at, updated_at FROM tasks WHERE id = $1",
        )
        .bind(task_id)
        .fetch_optional(pool.as_ref())
        .await
        .map_err(|e| EngineError::ConnectionError(format!("Task fetch error: {}", e)))?;

        match row {
            Some((id, description, project_id, status, subtasks_json, created_at, updated_at)) => {
                let subtasks: Vec<uc_types::Subtask> = serde_json::from_value(subtasks_json)
                    .unwrap_or_default();
                let task_status = task_status_from_str(&status)
                    .unwrap_or(TaskStatus::InProgress);
                Ok(Some(Task {
                    id: TaskId(id),
                    description,
                    project_id,
                    status: task_status,
                    subtasks,
                    created_at,
                    updated_at,
                }))
            }
            None => Ok(None),
        }
    }
}

#[cfg(feature = "storage")]
#[async_trait::async_trait]
impl TaskStoreBackend for PostgresTaskBackend {
    async fn submit_task(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let task = {
                let task_id = TaskId::new();
                let now = chrono::Utc::now();
                let subtasks = decompose_task(&task_id, &description);
                Task {
                    id: task_id,
                    description,
                    project_id,
                    status: TaskStatus::InProgress,
                    subtasks,
                    created_at: now,
                    updated_at: now,
                }
            };
            Self::upsert_task_pg(pool, &task).await?;
            Ok(task)
        } else {
            self.fallback.submit_task(description, project_id).await
        }
    }

    async fn submit_task_pending(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let task = {
                let task_id = TaskId::new();
                let now = chrono::Utc::now();
                Task {
                    id: task_id,
                    description,
                    project_id,
                    status: TaskStatus::Planning,
                    subtasks: Vec::new(),
                    created_at: now,
                    updated_at: now,
                }
            };
            Self::upsert_task_pg(pool, &task).await?;
            Ok(task)
        } else {
            self.fallback.submit_task_pending(description, project_id).await
        }
    }

    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError> {
        if let Some(pool) = &self.pool {
            Self::read_task_pg(pool, task_id).await
        } else {
            self.fallback.get_task(task_id).await
        }
    }

    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        if let Some(pool) = &self.pool {
            let rows = sqlx::query_as::<_, (String, String, String, String, serde_json::Value, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)>(
                "SELECT id, description, project_id, status, subtasks, created_at, updated_at FROM tasks ORDER BY created_at DESC",
            )
            .fetch_all(pool.as_ref())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Task list error: {}", e)))?;

            Ok(rows
                .into_iter()
                .map(|(id, description, project_id, status, subtasks_json, created_at, updated_at)| {
                    let subtasks: Vec<uc_types::Subtask> = serde_json::from_value(subtasks_json)
                        .unwrap_or_default();
                    let task_status = task_status_from_str(&status)
                        .unwrap_or(TaskStatus::InProgress);
                    Task {
                        id: TaskId(id),
                        description,
                        project_id,
                        status: task_status,
                        subtasks,
                        created_at,
                        updated_at,
                    }
                })
                .collect())
        } else {
            self.fallback.list_tasks().await
        }
    }

    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let task = Self::read_task_pg(pool, task_id)
                .await?
                .ok_or_else(|| EngineError::TaskError(format!("Task not found: {}", task_id)))?;

            match &task.status {
                TaskStatus::InProgress | TaskStatus::Planning => {
                    let now = chrono::Utc::now();
                    sqlx::query("UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3")
                        .bind("Paused")
                        .bind(now)
                        .bind(task_id)
                        .execute(pool.as_ref())
                        .await
                        .map_err(|e| EngineError::ConnectionError(format!("Task pause error: {}", e)))?;

                    Ok(Task {
                        status: TaskStatus::Paused,
                        updated_at: now,
                        ..task
                    })
                }
                other => Err(EngineError::TaskError(format!(
                    "Cannot pause task in {:?} status (expected InProgress or Planning)",
                    other
                ))),
            }
        } else {
            self.fallback.pause_task(task_id).await
        }
    }

    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let task = Self::read_task_pg(pool, task_id)
                .await?
                .ok_or_else(|| EngineError::TaskError(format!("Task not found: {}", task_id)))?;

            match &task.status {
                TaskStatus::Paused => {
                    let now = chrono::Utc::now();
                    sqlx::query("UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3")
                        .bind("InProgress")
                        .bind(now)
                        .bind(task_id)
                        .execute(pool.as_ref())
                        .await
                        .map_err(|e| EngineError::ConnectionError(format!("Task resume error: {}", e)))?;

                    Ok(Task {
                        status: TaskStatus::InProgress,
                        updated_at: now,
                        ..task
                    })
                }
                other => Err(EngineError::TaskError(format!(
                    "Cannot resume task in {:?} status (expected Paused)",
                    other
                ))),
            }
        } else {
            self.fallback.resume_task(task_id).await
        }
    }

    async fn apply_update(&self, update: &TaskUpdate) -> Result<(), EngineError> {
        if let Some(pool) = &self.pool {
            let task = match Self::read_task_pg(pool, &update.task_id).await? {
                Some(t) => t,
                None => {
                    tracing::warn!(
                        task_id = %update.task_id,
                        "Received update for unknown task, ignoring"
                    );
                    return Ok(());
                }
            };

            let mut updated_task = task;

            if let Some(status) = task_status_from_str(&update.status) {
                updated_task.status = status;
            }

            for subtask_update in &update.subtasks {
                if let Some(subtask) = updated_task
                    .subtasks
                    .iter_mut()
                    .find(|st| st.id.0 == subtask_update.subtask_id)
                {
                    if let Some(status) = subtask_status_from_str(&subtask_update.status) {
                        subtask.status = status;
                    }
                    if let Some(worker) = &subtask_update.assigned_worker {
                        subtask.assigned_worker = Some(uc_types::WorkerId(worker.clone()));
                    }
                } else {
                    let new_subtask = uc_types::Subtask {
                        id: TaskId(subtask_update.subtask_id.clone()),
                        parent_id: updated_task.id.clone(),
                        description: subtask_update.description.clone(),
                        status: subtask_status_from_str(&subtask_update.status)
                            .unwrap_or(uc_types::SubtaskStatus::Pending),
                        assigned_worker: subtask_update
                            .assigned_worker
                            .as_ref()
                            .map(|w| uc_types::WorkerId(w.clone())),
                        depends_on: subtask_update
                            .depends_on
                            .iter()
                            .map(|d| TaskId(d.clone()))
                            .collect(),
                        file_constraints: Vec::new(),
                        expected_output: String::new(),
                        result: None,
                    };
                    updated_task.subtasks.push(new_subtask);
                }
            }

            updated_task.updated_at = chrono::Utc::now();
            Self::upsert_task_pg(pool, &updated_task).await?;
            Ok(())
        } else {
            self.fallback.apply_update(update).await
        }
    }

    async fn set_task_status(
        &self,
        task_id: &str,
        new_status: TaskStatus,
    ) -> Result<Option<TaskStatus>, EngineError> {
        if let Some(pool) = &self.pool {
            let task = Self::read_task_pg(pool, task_id).await?;
            match task {
                Some(t) => {
                    let old = t.status.clone();
                    let now = chrono::Utc::now();
                    let status_str = task_status_to_str(&new_status);
                    sqlx::query("UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3")
                        .bind(status_str)
                        .bind(now)
                        .bind(task_id)
                        .execute(pool.as_ref())
                        .await
                        .map_err(|e| {
                            EngineError::ConnectionError(format!("Task status update error: {}", e))
                        })?;
                    Ok(Some(old))
                }
                None => Ok(None),
            }
        } else {
            self.fallback.set_task_status(task_id, new_status).await
        }
    }

    async fn update_last_heartbeat(&self) -> Result<(), EngineError> {
        self.fallback.update_last_heartbeat().await
    }

    async fn last_heartbeat(&self) -> Result<Option<chrono::DateTime<chrono::Utc>>, EngineError> {
        self.fallback.last_heartbeat().await
    }

    async fn mark_stale_tasks_failed(
        &self,
        timeout: std::time::Duration,
    ) -> Result<Vec<String>, EngineError> {
        let failed_ids = self.fallback.mark_stale_tasks_failed(timeout).await?;

        if let Some(pool) = &self.pool {
            let now = chrono::Utc::now();
            for id in &failed_ids {
                let _ = sqlx::query("UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3")
                    .bind("Failed")
                    .bind(now)
                    .bind(id)
                    .execute(pool.as_ref())
                    .await;
            }
        }

        Ok(failed_ids)
    }

    async fn remove_task(&self, task_id: &str) -> Result<(), EngineError> {
        if let Some(pool) = &self.pool {
            sqlx::query("DELETE FROM tasks WHERE id = $1")
                .bind(task_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::ConnectionError(format!("Task delete error: {}", e)))?;
        } else {
            self.fallback.remove_task(task_id).await?;
        }
        Ok(())
    }

    fn is_connected(&self) -> bool {
        self.pool.is_some()
    }
}

// ── PostgresTaskBackend (no storage feature) ─────────────────

/// When the `storage` feature is disabled, `PostgresTaskBackend` is just
/// a type alias for `InMemoryTaskBackend`.
#[cfg(not(feature = "storage"))]
pub type PostgresTaskBackend = InMemoryTaskBackend;

// ── Helper functions ─────────────────────────────────────────

/// Parse a TaskStatus from its string representation.
pub fn task_status_from_str(s: &str) -> Option<TaskStatus> {
    match s {
        "Created" => Some(TaskStatus::Created),
        "Planning" => Some(TaskStatus::Planning),
        "InProgress" => Some(TaskStatus::InProgress),
        "Completed" => Some(TaskStatus::Completed),
        "Failed" => Some(TaskStatus::Failed),
        "Paused" => Some(TaskStatus::Paused),
        _ => None,
    }
}

/// Convert a TaskStatus to its string representation.
pub fn task_status_to_str(status: &TaskStatus) -> &'static str {
    match status {
        TaskStatus::Created => "Created",
        TaskStatus::Planning => "Planning",
        TaskStatus::InProgress => "InProgress",
        TaskStatus::Completed => "Completed",
        TaskStatus::Failed => "Failed",
        TaskStatus::Paused => "Paused",
    }
}

/// Parse a SubtaskStatus from its string representation.
pub fn subtask_status_from_str(s: &str) -> Option<uc_types::SubtaskStatus> {
    match s {
        "Pending" => Some(uc_types::SubtaskStatus::Pending),
        "Assigned" => Some(uc_types::SubtaskStatus::Assigned),
        "InProgress" => Some(uc_types::SubtaskStatus::InProgress),
        "Completed" => Some(uc_types::SubtaskStatus::Completed),
        "Failed" => Some(uc_types::SubtaskStatus::Failed),
        "Conflicted" => Some(uc_types::SubtaskStatus::Conflicted),
        _ => None,
    }
}

/// Simple task decomposition heuristic: split description by newlines
/// or numbered items, creating one subtask per line/item.
pub fn decompose_task(
    parent_id: &TaskId,
    description: &str,
) -> Vec<uc_types::Subtask> {
    let lines: Vec<&str> = description
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();

    if lines.is_empty() {
        return vec![uc_types::Subtask {
            id: TaskId::new(),
            parent_id: parent_id.clone(),
            description: description.to_string(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
        }];
    }

    let mut subtasks = Vec::new();
    let mut prev_id: Option<TaskId> = None;

    for (i, line) in lines.iter().enumerate() {
        let cleaned = line
            .trim_start_matches(|c: char| c.is_numeric())
            .trim_start_matches(['.', ')', ' '])
            .to_string();

        let desc = if cleaned.is_empty() {
            line.to_string()
        } else {
            cleaned
        };

        let st_id = TaskId::new();
        let depends_on = if i > 0 {
            prev_id
                .as_ref()
                .map(|id| vec![id.clone()])
                .unwrap_or_default()
        } else {
            Vec::new()
        };

        subtasks.push(uc_types::Subtask {
            id: st_id.clone(),
            parent_id: parent_id.clone(),
            description: desc,
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on,
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
        });

        prev_id = Some(st_id);
    }

    subtasks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn in_memory_submit_and_get() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task(
                "1. Analyze code\n2. Fix bug\n3. Write tests".to_string(),
                "project-1".to_string(),
            )
            .await
            .unwrap();

        assert_eq!(task.subtasks.len(), 3);
        assert_eq!(task.status, TaskStatus::InProgress);

        let retrieved = store.get_task(&task.id.0).await.unwrap();
        assert!(retrieved.is_some());
        assert_eq!(
            retrieved.unwrap().description,
            "1. Analyze code\n2. Fix bug\n3. Write tests"
        );
    }

    #[tokio::test]
    async fn in_memory_submit_pending() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task_pending("Fix the login bug".to_string(), "project-1".to_string())
            .await
            .unwrap();

        assert_eq!(task.status, TaskStatus::Planning);
        assert!(task.subtasks.is_empty());
    }

    #[tokio::test]
    async fn in_memory_pause_and_resume() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task("Test task".to_string(), "p1".to_string())
            .await
            .unwrap();
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).await.unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        let resumed = store.resume_task(&task_id).await.unwrap();
        assert_eq!(resumed.status, TaskStatus::InProgress);
    }

    #[tokio::test]
    async fn in_memory_pause_nonexistent() {
        let store = InMemoryTaskBackend::new();
        let result = store.pause_task("nonexistent").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn in_memory_list_tasks() {
        let store = InMemoryTaskBackend::new();
        store
            .submit_task("Task 1".to_string(), "p1".to_string())
            .await
            .unwrap();
        store
            .submit_task("Task 2".to_string(), "p1".to_string())
            .await
            .unwrap();

        let tasks = store.list_tasks().await.unwrap();
        assert_eq!(tasks.len(), 2);
    }

    #[tokio::test]
    async fn in_memory_apply_update() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task("Test task".to_string(), "p1".to_string())
            .await
            .unwrap();
        let task_id = task.id.0.clone();
        let subtask_id = task.subtasks[0].id.0.clone();

        let update = TaskUpdate {
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            description: "Test task".to_string(),
            project_id: "p1".to_string(),
            subtasks: vec![SubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "Completed".to_string(),
                description: String::new(),
                assigned_worker: None,
                depends_on: vec![],
            }],
            result: None,
        };

        store.apply_update(&update).await.unwrap();

        let updated = store.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(updated.subtasks[0].status, uc_types::SubtaskStatus::Completed);
    }

    #[tokio::test]
    async fn in_memory_set_task_status() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task("Test task".to_string(), "p1".to_string())
            .await
            .unwrap();
        let task_id = task.id.0.clone();

        let old = store
            .set_task_status(&task_id, TaskStatus::Failed)
            .await
            .unwrap();
        assert_eq!(old, Some(TaskStatus::InProgress));

        let updated = store.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(updated.status, TaskStatus::Failed);
    }

    #[tokio::test]
    async fn in_memory_heartbeat_tracking() {
        let store = InMemoryTaskBackend::new();

        assert!(store.last_heartbeat().await.unwrap().is_none());

        store.update_last_heartbeat().await.unwrap();
        assert!(store.last_heartbeat().await.unwrap().is_some());
    }

    #[tokio::test]
    async fn in_memory_mark_stale_tasks_failed() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task("Test task".to_string(), "p1".to_string())
            .await
            .unwrap();

        store.update_last_heartbeat().await.unwrap();

        let failed = store
            .mark_stale_tasks_failed(std::time::Duration::ZERO)
            .await
            .unwrap();
        assert_eq!(failed.len(), 1);
        assert_eq!(failed[0], task.id.0);

        let updated = store.get_task(&task.id.0).await.unwrap().unwrap();
        assert_eq!(updated.status, TaskStatus::Failed);
    }

    #[tokio::test]
    async fn in_memory_remove_task() {
        let store = InMemoryTaskBackend::new();
        let task = store
            .submit_task("Test task".to_string(), "p1".to_string())
            .await
            .unwrap();

        store.remove_task(&task.id.0).await.unwrap();
        assert!(store.get_task(&task.id.0).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn in_memory_is_connected() {
        let store = InMemoryTaskBackend::new();
        assert!(!store.is_connected());
    }

    #[test]
    fn task_status_roundtrip() {
        let statuses = [
            TaskStatus::Created,
            TaskStatus::Planning,
            TaskStatus::InProgress,
            TaskStatus::Completed,
            TaskStatus::Failed,
            TaskStatus::Paused,
        ];
        for status in &statuses {
            let s = task_status_to_str(status);
            let parsed = task_status_from_str(s).unwrap();
            assert_eq!(*status, parsed);
        }
    }

    #[test]
    fn task_status_from_str_unknown() {
        assert!(task_status_from_str("Unknown").is_none());
        assert!(task_status_from_str("").is_none());
    }

    #[test]
    fn subtask_status_roundtrip() {
        let statuses = [
            uc_types::SubtaskStatus::Pending,
            uc_types::SubtaskStatus::Assigned,
            uc_types::SubtaskStatus::InProgress,
            uc_types::SubtaskStatus::Completed,
            uc_types::SubtaskStatus::Failed,
            uc_types::SubtaskStatus::Conflicted,
        ];
        for status in &statuses {
            let s = match status {
                uc_types::SubtaskStatus::Pending => "Pending",
                uc_types::SubtaskStatus::Assigned => "Assigned",
                uc_types::SubtaskStatus::InProgress => "InProgress",
                uc_types::SubtaskStatus::Completed => "Completed",
                uc_types::SubtaskStatus::Failed => "Failed",
                uc_types::SubtaskStatus::Conflicted => "Conflicted",
            };
            let parsed = subtask_status_from_str(s).unwrap();
            assert_eq!(*status, parsed);
        }
    }

    #[test]
    fn decompose_task_single_line() {
        let parent_id = TaskId::new();
        let subtasks = decompose_task(&parent_id, "Single task description");
        assert_eq!(subtasks.len(), 1);
        assert_eq!(subtasks[0].description, "Single task description");
    }

    #[test]
    fn decompose_task_multiple_lines() {
        let parent_id = TaskId::new();
        let subtasks = decompose_task(&parent_id, "1. First item\n2. Second item\n3. Third item");
        assert_eq!(subtasks.len(), 3);
        assert_eq!(subtasks[0].description, "First item");
        assert_eq!(subtasks[1].description, "Second item");
        assert!(subtasks[0].depends_on.is_empty());
        assert_eq!(subtasks[1].depends_on.len(), 1);
    }
}
