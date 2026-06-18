// Legacy synchronous TaskStore for LocalEngine backward compatibility.
//
// The `TaskStore` struct is a thin synchronous wrapper that the
// `LocalEngine` uses for its `EngineApi` task methods. For the new
// async `TaskStoreBackend` trait and PostgreSQL persistence, see
// the `mod.rs` in this module.

use std::collections::HashMap;
use uc_types::{Task, TaskId, TaskStatus};

/// In-memory store for tasks, used by LocalEngine to implement
/// the task orchestration methods on the EngineApi trait.
///
/// This is the legacy synchronous struct. For the new async
/// `TaskStoreBackend` trait, use `InMemoryTaskBackend` instead.
pub struct TaskStore {
    tasks: HashMap<String, Task>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskStore {
    /// Create a new empty task store.
    pub fn new() -> Self {
        Self {
            tasks: HashMap::new(),
        }
    }

    /// Submit a new task: create it, decompose into subtasks, store, and return.
    pub fn submit_task(&mut self, description: String, project_id: String) -> Task {
        let task_id = TaskId::new();
        let now = chrono::Utc::now();

        let subtasks = super::decompose_task(&task_id, &description);

        let task = Task {
            id: task_id.clone(),
            description,
            project_id,
            status: TaskStatus::InProgress,
            subtasks,
            created_at: now,
            updated_at: now,
        };

        self.tasks.insert(task_id.0.clone(), task.clone());
        task
    }

    /// Get a task by ID.
    pub fn get_task(&self, task_id: &str) -> Option<&Task> {
        self.tasks.get(task_id)
    }

    /// List all tasks.
    pub fn list_tasks(&self) -> Vec<Task> {
        self.tasks.values().cloned().collect()
    }

    /// Pause a task. Only tasks in InProgress or Planning status can be paused.
    pub fn pause_task(&mut self, task_id: &str) -> Result<Task, String> {
        let task = self
            .tasks
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

    /// Resume a task. Only tasks in Paused status can be resumed.
    pub fn resume_task(&mut self, task_id: &str) -> Result<Task, String> {
        let task = self
            .tasks
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_store_submit_and_get() {
        let mut store = TaskStore::new();
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
        let mut store = TaskStore::new();
        store.submit_task("Task 1".to_string(), "p1".to_string());
        store.submit_task("Task 2".to_string(), "p2".to_string());

        let tasks = store.list_tasks();
        assert_eq!(tasks.len(), 2);
    }

    #[test]
    fn task_store_pause_and_resume() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        let resumed = store.resume_task(&task_id).unwrap();
        assert_eq!(resumed.status, TaskStatus::InProgress);
    }

    #[test]
    fn task_store_pause_nonexistent() {
        let mut store = TaskStore::new();
        let result = store.pause_task("nonexistent");
        assert!(result.is_err());
    }

    #[test]
    fn task_store_pause_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        let result = store.pause_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_resume_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let result = store.resume_task(&task_id);
        assert!(result.is_err());
    }
}
