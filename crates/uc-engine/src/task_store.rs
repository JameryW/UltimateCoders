//! Task store backends — trait definition, in-memory implementation, PostgreSQL implementation.
//!
//! The `TaskStoreBackend` trait provides an async interface for task persistence.
//! `InMemoryTaskBackend` is always available (HashMap-based).
//! `PostgresTaskBackend` is feature-gated on `storage`.

use std::collections::HashMap;
use uc_types::error::EngineError;
use uc_types::{Subtask, SubtaskStatus, Task, TaskId, TaskStatus};

#[cfg(feature = "storage")]
use std::sync::Arc;

#[cfg(feature = "storage")]
use sqlx::postgres::PgPool;

// ── Trait ──────────────────────────────────────────────────────

/// Async trait for task store backends.
///
/// Implementations: `InMemoryTaskBackend` (always), `PostgresTaskBackend` (feature-gated).
#[async_trait::async_trait]
pub trait TaskStoreBackend: Send + Sync {
    /// Submit a new task (already constructed).
    async fn submit_task(&self, task: Task) -> Result<Task, EngineError>;

    /// Get a task by ID.
    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError>;

    /// List all tasks.
    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError>;

    /// Update a task (upsert).
    async fn update_task(&self, task: Task) -> Result<Task, EngineError>;

    /// Pause a task.
    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// Resume a task.
    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// Delete a task.
    async fn delete_task(&self, task_id: &str) -> Result<(), EngineError>;
}

// ── In-memory backend ──────────────────────────────────────────

/// In-memory task store backed by HashMap.
///
/// Always available, used for testing and as PostgreSQL fallback.
pub struct InMemoryTaskBackend {
    tasks: tokio::sync::RwLock<HashMap<String, Task>>,
}

impl Default for InMemoryTaskBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl InMemoryTaskBackend {
    pub fn new() -> Self {
        Self {
            tasks: tokio::sync::RwLock::new(HashMap::new()),
        }
    }
}

#[async_trait::async_trait]
impl TaskStoreBackend for InMemoryTaskBackend {
    async fn submit_task(&self, task: Task) -> Result<Task, EngineError> {
        let task_id = task.id.0.clone();
        self.tasks.write().await.insert(task_id, task.clone());
        Ok(task)
    }

    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError> {
        Ok(self.tasks.read().await.get(task_id).cloned())
    }

    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        Ok(self.tasks.read().await.values().cloned().collect())
    }

    async fn update_task(&self, task: Task) -> Result<Task, EngineError> {
        let task_id = task.id.0.clone();
        self.tasks.write().await.insert(task_id, task.clone());
        Ok(task)
    }

    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut tasks = self.tasks.write().await;
        let task = tasks
            .get_mut(task_id)
            .ok_or_else(|| EngineError::NotFound(format!("Task not found: {}", task_id)))?;
        match &task.status {
            TaskStatus::InProgress | TaskStatus::Planning => {
                task.status = TaskStatus::Paused;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(EngineError::InvalidOperation(format!(
                "Cannot pause task in {:?} status",
                other
            ))),
        }
    }

    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut tasks = self.tasks.write().await;
        let task = tasks
            .get_mut(task_id)
            .ok_or_else(|| EngineError::NotFound(format!("Task not found: {}", task_id)))?;
        match &task.status {
            TaskStatus::Paused => {
                task.status = TaskStatus::InProgress;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(EngineError::InvalidOperation(format!(
                "Cannot resume task in {:?} status",
                other
            ))),
        }
    }

    async fn delete_task(&self, task_id: &str) -> Result<(), EngineError> {
        self.tasks
            .write()
            .await
            .remove(task_id)
            .map(|_| ())
            .ok_or_else(|| EngineError::NotFound(format!("Task not found: {}", task_id)))
    }
}

// ── PostgreSQL backend ─────────────────────────────────────────

/// PostgreSQL-backed task store.
///
/// Feature-gated on `storage`. Falls back to `InMemoryTaskBackend` when PG is unavailable.
#[cfg(feature = "storage")]
pub struct PostgresTaskBackend {
    pool: Option<Arc<PgPool>>,
    fallback: InMemoryTaskBackend,
}

#[cfg(feature = "storage")]
impl PostgresTaskBackend {
    /// Create a new PostgreSQL task store, connecting to the database.
    pub async fn new(database_url: &str) -> Result<Self, EngineError> {
        let pool = match sqlx::postgres::PgPoolOptions::new()
            .max_connections(5)
            .connect(database_url)
            .await
        {
            Ok(pool) => {
                // Run migrations
                Self::run_migrations(&pool).await?;
                tracing::info!("Connected to PostgreSQL for task storage");
                Some(Arc::new(pool))
            }
            Err(e) => {
                tracing::warn!(
                    "PostgreSQL unavailable, using in-memory fallback for tasks: {}",
                    e
                );
                None
            }
        };

        Ok(Self {
            pool,
            fallback: InMemoryTaskBackend::new(),
        })
    }

    /// Create with fallback only (no PostgreSQL).
    pub fn new_fallback() -> Self {
        Self {
            pool: None,
            fallback: InMemoryTaskBackend::new(),
        }
    }

    /// Create with an existing pool.
    pub fn with_pool(pool: Arc<PgPool>) -> Self {
        Self {
            pool: Some(pool),
            fallback: InMemoryTaskBackend::new(),
        }
    }

    async fn run_migrations(pool: &PgPool) -> Result<(), EngineError> {
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
        .execute(pool)
        .await
        .map_err(|e| EngineError::StorageError(format!("Task migration failed: {}", e)))?;

        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS agent_events (
                id BIGSERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                "offset" BIGINT NOT NULL,
                subject TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            "#,
        )
        .execute(pool)
        .await
        .map_err(|e| EngineError::StorageError(format!("Task migration failed: {}", e)))?;

        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_agent_events_task_id ON agent_events(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_events_subject ON agent_events(subject)",
        ] {
            sqlx::query(idx_sql).execute(pool).await.map_err(|e| {
                EngineError::StorageError(format!("Task index creation failed: {}", e))
            })?;
        }
        Ok(())
    }

    /// Read a task from a PostgreSQL row.
    fn row_to_task(
        id: String,
        description: String,
        project_id: String,
        status: String,
        subtasks_json: serde_json::Value,
        created_at: chrono::DateTime<chrono::Utc>,
        updated_at: chrono::DateTime<chrono::Utc>,
    ) -> Result<Task, EngineError> {
        let status = match status.as_str() {
            "Created" => TaskStatus::Created,
            "Planning" => TaskStatus::Planning,
            "InProgress" => TaskStatus::InProgress,
            "Paused" => TaskStatus::Paused,
            "Completed" => TaskStatus::Completed,
            "Failed" => TaskStatus::Failed,
            other => {
                return Err(EngineError::StorageError(format!(
                    "Unknown task status: {}",
                    other
                )))
            }
        };

        let subtasks: Vec<Subtask> = serde_json::from_value(subtasks_json)
            .map_err(|e| EngineError::StorageError(format!("Subtask deserialization: {}", e)))?;

        Ok(Task {
            id: TaskId(id),
            description,
            project_id,
            status,
            subtasks,
            created_at,
            updated_at,
        })
    }

    fn task_status_str(status: &TaskStatus) -> &'static str {
        match status {
            TaskStatus::Created => "Created",
            TaskStatus::Planning => "Planning",
            TaskStatus::InProgress => "InProgress",
            TaskStatus::Paused => "Paused",
            TaskStatus::Completed => "Completed",
            TaskStatus::Failed => "Failed",
        }
    }
}

#[cfg(feature = "storage")]
#[async_trait::async_trait]
impl TaskStoreBackend for PostgresTaskBackend {
    async fn submit_task(&self, task: Task) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let subtasks_json = serde_json::to_value(&task.subtasks)
                .map_err(|e| EngineError::StorageError(format!("Subtask serialization: {}", e)))?;
            sqlx::query(
                "INSERT INTO tasks (id, description, project_id, status, subtasks, created_at, updated_at) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7)"
            )
                .bind(&task.id.0)
                .bind(&task.description)
                .bind(&task.project_id)
                .bind(Self::task_status_str(&task.status))
                .bind(&subtasks_json)
                .bind(task.created_at)
                .bind(task.updated_at)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task insert failed: {}", e)))?;
            Ok(task)
        } else {
            self.fallback.submit_task(task).await
        }
    }

    async fn get_task(&self, task_id: &str) -> Result<Option<Task>, EngineError> {
        if let Some(pool) = &self.pool {
            let row = sqlx::query_as::<_, (String, String, String, String, serde_json::Value, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)>(
                "SELECT id, description, project_id, status, subtasks, created_at, updated_at FROM tasks WHERE id = $1"
            )
                .bind(task_id)
                .fetch_optional(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task select failed: {}", e)))?;

            match row {
                Some((id, desc, proj, status, st_json, created, updated)) => Ok(Some(
                    Self::row_to_task(id, desc, proj, status, st_json, created, updated)?,
                )),
                None => Ok(None),
            }
        } else {
            self.fallback.get_task(task_id).await
        }
    }

    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        if let Some(pool) = &self.pool {
            let rows = sqlx::query_as::<_, (String, String, String, String, serde_json::Value, chrono::DateTime<chrono::Utc>, chrono::DateTime<chrono::Utc>)>(
                "SELECT id, description, project_id, status, subtasks, created_at, updated_at FROM tasks ORDER BY created_at DESC"
            )
                .fetch_all(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task list failed: {}", e)))?;

            rows.into_iter()
                .map(|(id, desc, proj, status, st_json, created, updated)| {
                    Self::row_to_task(id, desc, proj, status, st_json, created, updated)
                })
                .collect()
        } else {
            self.fallback.list_tasks().await
        }
    }

    async fn update_task(&self, task: Task) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let subtasks_json = serde_json::to_value(&task.subtasks)
                .map_err(|e| EngineError::StorageError(format!("Subtask serialization: {}", e)))?;
            sqlx::query(
                "UPDATE tasks SET description = $2, project_id = $3, status = $4, subtasks = $5, updated_at = $6 WHERE id = $1"
            )
                .bind(&task.id.0)
                .bind(&task.description)
                .bind(&task.project_id)
                .bind(Self::task_status_str(&task.status))
                .bind(&subtasks_json)
                .bind(task.updated_at)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task update failed: {}", e)))?;
            Ok(task)
        } else {
            self.fallback.update_task(task).await
        }
    }

    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let result = sqlx::query(
                "UPDATE tasks SET status = 'Paused', updated_at = NOW() WHERE id = $1 AND status IN ('InProgress', 'Planning')"
            )
                .bind(task_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task pause failed: {}", e)))?;

            if result.rows_affected() == 0 {
                return Err(EngineError::InvalidOperation(format!(
                    "Cannot pause task {} (not found or wrong status)",
                    task_id
                )));
            }
            self.get_task(task_id).await.map(|t| t.unwrap())
        } else {
            self.fallback.pause_task(task_id).await
        }
    }

    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        if let Some(pool) = &self.pool {
            let result = sqlx::query(
                "UPDATE tasks SET status = 'InProgress', updated_at = NOW() WHERE id = $1 AND status = 'Paused'"
            )
                .bind(task_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task resume failed: {}", e)))?;

            if result.rows_affected() == 0 {
                return Err(EngineError::InvalidOperation(format!(
                    "Cannot resume task {} (not found or not paused)",
                    task_id
                )));
            }
            self.get_task(task_id).await.map(|t| t.unwrap())
        } else {
            self.fallback.resume_task(task_id).await
        }
    }

    async fn delete_task(&self, task_id: &str) -> Result<(), EngineError> {
        if let Some(pool) = &self.pool {
            sqlx::query("DELETE FROM tasks WHERE id = $1")
                .bind(task_id)
                .execute(pool.as_ref())
                .await
                .map_err(|e| EngineError::StorageError(format!("Task delete failed: {}", e)))?;
            Ok(())
        } else {
            self.fallback.delete_task(task_id).await
        }
    }
}

// ── Legacy sync TaskStore (for LocalEngine compatibility) ──────

/// In-memory store for tasks, used by LocalEngine.
///
/// Uses `std::sync::RwLock` internally — fully synchronous, no async runtime needed.
pub struct TaskStore {
    tasks: std::sync::RwLock<HashMap<String, Task>>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskStore {
    pub fn new() -> Self {
        Self {
            tasks: std::sync::RwLock::new(HashMap::new()),
        }
    }

    /// Submit a new task: create it, decompose into subtasks, store, and return.
    pub fn submit_task(&mut self, description: String, project_id: String) -> Task {
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
        self.tasks
            .write()
            .expect("task_store lock poisoned")
            .insert(task_id.0.clone(), task.clone());
        task
    }

    /// Get a task by ID (owned).
    pub fn get_task(&self, task_id: &str) -> Option<Task> {
        self.tasks
            .read()
            .expect("task_store lock poisoned")
            .get(task_id)
            .cloned()
    }

    /// List all tasks.
    pub fn list_tasks(&self) -> Vec<Task> {
        self.tasks
            .read()
            .expect("task_store lock poisoned")
            .values()
            .cloned()
            .collect()
    }

    /// Pause a task.
    pub fn pause_task(&mut self, task_id: &str) -> Result<Task, String> {
        let mut tasks = self.tasks.write().expect("task_store lock poisoned");
        let task = tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            TaskStatus::InProgress | TaskStatus::Planning => {
                task.status = TaskStatus::Paused;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(format!(
                "Cannot pause task in {:?} status (expected InProgress or Planning)",
                other
            )),
        }
    }

    /// Resume a task.
    pub fn resume_task(&mut self, task_id: &str) -> Result<Task, String> {
        let mut tasks = self.tasks.write().expect("task_store lock poisoned");
        let task = tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            TaskStatus::Paused => {
                task.status = TaskStatus::InProgress;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(format!(
                "Cannot resume task in {:?} status (expected Paused)",
                other
            )),
        }
    }

    /// Convert to an `InMemoryTaskBackend` (for migration to async API).
    pub fn into_backend(self) -> InMemoryTaskBackend {
        let tasks = self.tasks.into_inner().expect("task_store lock poisoned");
        let backend = InMemoryTaskBackend::new();
        // ponytail: bulk-insert — acceptable for one-time migration
        let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
        for (_, task) in tasks {
            rt.block_on(backend.submit_task(task))
                .expect("submit_task infallible");
        }
        backend
    }
}

/// Simple task decomposition heuristic: split description by newlines
/// or numbered items, creating one subtask per line/item.
fn decompose_task(parent_id: &TaskId, description: &str) -> Vec<Subtask> {
    let lines: Vec<&str> = description
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();

    if lines.is_empty() {
        return vec![Subtask {
            id: TaskId::new(),
            parent_id: parent_id.clone(),
            description: description.to_string(),
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            dispatch_retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
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

        subtasks.push(Subtask {
            id: st_id.clone(),
            parent_id: parent_id.clone(),
            description: desc,
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on,
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            dispatch_retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        });

        prev_id = Some(st_id);
    }

    subtasks
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_store() -> TaskStore {
        TaskStore::new()
    }

    #[test]
    fn task_store_submit_and_get() {
        let mut store = make_store();
        let task = store.submit_task(
            "1. Analyze code\n2. Fix bug\n3. Write tests".to_string(),
            "project-1".to_string(),
        );

        assert_eq!(task.subtasks.len(), 3);
        assert_eq!(task.status, TaskStatus::InProgress);

        let retrieved = store.get_task(&task.id.0).unwrap();
        assert_eq!(
            retrieved.description,
            "1. Analyze code\n2. Fix bug\n3. Write tests"
        );
    }

    #[test]
    fn task_store_list_tasks() {
        let mut store = make_store();
        store.submit_task("Task 1".to_string(), "p1".to_string());
        store.submit_task("Task 2".to_string(), "p2".to_string());

        let tasks = store.list_tasks();
        assert_eq!(tasks.len(), 2);
    }

    #[test]
    fn task_store_pause_and_resume() {
        let mut store = make_store();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        let resumed = store.resume_task(&task_id).unwrap();
        assert_eq!(resumed.status, TaskStatus::InProgress);
    }

    #[test]
    fn task_store_pause_nonexistent() {
        let mut store = make_store();
        let result = store.pause_task("nonexistent");
        assert!(result.is_err());
    }

    #[test]
    fn task_store_pause_invalid_status() {
        let mut store = make_store();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        let result = store.pause_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_resume_invalid_status() {
        let mut store = make_store();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let result = store.resume_task(&task_id);
        assert!(result.is_err());
    }

    // ── Async backend tests ────────────────────────────────────

    #[tokio::test]
    async fn in_memory_backend_crud() {
        let backend = InMemoryTaskBackend::new();

        let task = Task {
            id: TaskId::new(),
            description: "Test task".to_string(),
            project_id: "p1".to_string(),
            status: TaskStatus::InProgress,
            subtasks: Vec::new(),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        let task_id = task.id.0.clone();

        // Submit
        backend.submit_task(task.clone()).await.unwrap();

        // Get
        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.description, "Test task");

        // List
        let all = backend.list_tasks().await.unwrap();
        assert_eq!(all.len(), 1);

        // Update
        let mut updated = got;
        updated.description = "Updated".to_string();
        backend.update_task(updated).await.unwrap();
        let got2 = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got2.description, "Updated");

        // Pause
        let paused = backend.pause_task(&task_id).await.unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        // Resume
        let resumed = backend.resume_task(&task_id).await.unwrap();
        assert_eq!(resumed.status, TaskStatus::InProgress);

        // Delete
        backend.delete_task(&task_id).await.unwrap();
        assert!(backend.get_task(&task_id).await.unwrap().is_none());
    }
}
