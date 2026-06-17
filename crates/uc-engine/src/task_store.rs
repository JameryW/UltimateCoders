//! In-memory task store for LocalEngine.
//!
//! This is a simplified version of the TaskStore in uc-grpc server.
//! It provides basic task lifecycle management without NATS integration
//! or local worker bridge.

use std::collections::HashMap;
use uc_types::{Subtask, SubtaskStatus, Task, TaskId, TaskStatus};

/// In-memory store for tasks, used by LocalEngine to implement
/// the task orchestration methods on the EngineApi trait.
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

        // Simple decomposition: split by newlines, creating one subtask per line
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

/// Simple task decomposition heuristic: split description by newlines
/// or numbered items, creating one subtask per line/item.
fn decompose_task(parent_id: &TaskId, description: &str) -> Vec<Subtask> {
    let lines: Vec<&str> = description
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();

    if lines.is_empty() {
        // Single subtask if description has no newlines
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
        }];
    }

    // Create subtasks from lines, with sequential dependencies
    let mut subtasks = Vec::new();
    let mut prev_id: Option<TaskId> = None;

    for (i, line) in lines.iter().enumerate() {
        // Strip leading numbers like "1. " or "1) "
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
        });

        prev_id = Some(st_id);
    }

    subtasks
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

        // Get the task back
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

        // Pause (valid: InProgress -> Paused)
        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, TaskStatus::Paused);

        // Pause again (invalid: Paused -> Paused)
        let result = store.pause_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_resume_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Resume without pausing (invalid: InProgress -> InProgress)
        let result = store.resume_task(&task_id);
        assert!(result.is_err());
    }
}
