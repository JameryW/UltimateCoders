//! Agent types for the Orchestrator-Worker system.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Unique identifier for a worker agent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct WorkerId(pub String);

impl WorkerId {
    pub fn new() -> Self {
        Self(Uuid::new_v4().to_string())
    }
}

impl Default for WorkerId {
    fn default() -> Self {
        Self::new()
    }
}

/// Unique identifier for a task.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct TaskId(pub String);

impl TaskId {
    pub fn new() -> Self {
        Self(Uuid::new_v4().to_string())
    }
}

impl Default for TaskId {
    fn default() -> Self {
        Self::new()
    }
}

/// A top-level task submitted by the user.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: TaskId,
    pub description: String,
    /// The project/repository context.
    pub project_id: String,
    pub status: TaskStatus,
    pub subtasks: Vec<Subtask>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
}

/// Status of a task.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum TaskStatus {
    Created,
    Planning,
    InProgress,
    Completed,
    Failed,
    Paused,
}

/// A subtask assigned to a worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Subtask {
    pub id: TaskId,
    pub parent_id: TaskId,
    pub description: String,
    pub status: SubtaskStatus,
    /// Which worker is assigned (None if not yet assigned).
    pub assigned_worker: Option<WorkerId>,
    /// Dependencies on other subtasks (must complete before this one).
    pub depends_on: Vec<TaskId>,
    /// Constraints: files that should NOT be modified.
    pub file_constraints: Vec<String>,
    /// Expected output description.
    pub expected_output: String,
    /// Result from the worker.
    pub result: Option<SubtaskResult>,
}

/// Status of a subtask.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum SubtaskStatus {
    Pending,
    Assigned,
    InProgress,
    Completed,
    Failed,
    Conflicted,
}

/// Result from a completed subtask.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubtaskResult {
    pub subtask_id: TaskId,
    pub worker_id: WorkerId,
    /// Files modified by the worker.
    pub modified_files: Vec<FileChange>,
    /// Summary of what was done.
    pub summary: String,
    /// Whether the subtask succeeded.
    pub success: bool,
    pub completed_at: chrono::DateTime<chrono::Utc>,
}

/// A file change produced by a worker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileChange {
    pub file_path: String,
    pub change_type: ChangeType,
    /// Unified diff of the change.
    pub diff: String,
}

/// Type of file change.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ChangeType {
    Created,
    Modified,
    Deleted,
}

/// Worker registration info.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerInfo {
    pub id: WorkerId,
    /// Capabilities (languages, frameworks, tools).
    pub capabilities: Vec<String>,
    /// Current load (number of active subtasks).
    pub current_load: u32,
    /// Maximum concurrent subtasks.
    pub max_capacity: u32,
    pub last_heartbeat: chrono::DateTime<chrono::Utc>,
}

/// An event in the agent event stream (for Event Sourcing).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentEvent {
    pub event_id: u64,
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub payload: AgentEventPayload,
}

/// Payload of an agent event.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AgentEventPayload {
    TaskCreated { task: Task },
    SubtaskAssigned { subtask_id: TaskId, worker_id: WorkerId },
    WorkerStarted { subtask_id: TaskId, worker_id: WorkerId },
    ToolInvoked { subtask_id: TaskId, tool_name: String, tool_input: String },
    ToolResult { subtask_id: TaskId, tool_output: String, exit_code: i32 },
    FileModified { subtask_id: TaskId, file_path: String, diff: String },
    SubtaskCompleted { result: SubtaskResult },
    SubtaskFailed { subtask_id: TaskId, error: String, recoverable: bool },
    CheckpointCreated { task_id: TaskId, snapshot_id: String },
    EditIntent { worker_id: WorkerId, file_path: String, regions: Vec<(u32, u32)> },
    ConflictDetected { file_path: String, workers: Vec<WorkerId> },
}
