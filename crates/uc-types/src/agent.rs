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

/// Dispatch mode for a subtask — controls how it is routed to workers.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum DispatchMode {
    /// Execute locally (reserved for future use; currently no-op).
    Local,
    /// Must execute on a remote worker via NATS. Revert to Pending on
    /// NATS failure with retry_count increment; mark Failed after 3 retries.
    Remote,
    /// Prefer remote dispatch; fall back to Pending on NATS failure (default).
    #[default]
    PreferRemote,
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
    /// How this subtask should be dispatched (local / remote / prefer-remote).
    #[serde(default)]
    pub dispatch_mode: DispatchMode,
    /// How many times dispatch has been retried (for Remote mode).
    #[serde(default)]
    pub dispatch_retry_count: u32,
    /// Capabilities required by this subtask (e.g., "rust", "python", "docker").
    /// Worker must possess ALL listed capabilities to accept this subtask.
    /// Empty list means any worker can accept (backward compatible).
    #[serde(default)]
    pub required_capabilities: Vec<String>,
    /// Per-subtask agent configuration overrides (JSON string).
    /// Keys: tools, allowed_tools, disallowed_tools, mcp_configs,
    ///       append_system_prompt, agent_name, agents_json.
    #[serde(default)]
    pub agent_config_json: Option<String>,
    /// Ordered multi-agent workflow steps. Empty (default) = single-agent
    /// execution via `agent_config_json` (backward compatible). When non-empty,
    /// the worker runs steps in order, threading each step's output into the
    /// next step's prompt template (`{{prev_summary}}`, `{{prev_files}}`,
    /// `{{step<N>.summary}}`). Typical chain: claude-code write → codex CR →
    /// claude-code revise.
    #[serde(default)]
    pub steps: Vec<WorkflowStep>,
}

/// A single step in a subtask's multi-agent workflow.
///
/// Each step runs one coding agent (claude-code / codex) with a prompt
/// template. Steps run sequentially; the previous step's AgentOutput is
/// available to the next step's prompt via template variables.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct WorkflowStep {
    /// Agent adapter name ("claude-code" | "codex").
    pub agent: String,
    /// Prompt template. Supports:
    ///   {{prev_summary}} — previous step's AgentOutput.summary
    ///   {{prev_files}}   — previous step's modified file paths (one per line)
    ///   {{step0.summary}}, {{step0.files}} ... — any prior step by index
    /// Step 0 has no prev; {{prev_*}} resolves to empty for it.
    pub prompt: String,
    /// Per-step agent config overrides (same JSON shape as Subtask::agent_config_json).
    #[serde(default)]
    pub agent_config_json: Option<String>,
    /// If true (default), a failed step aborts the whole chain and the
    /// subtask fails. If false, the chain continues to the next step.
    #[serde(default = "default_true")]
    pub abort_on_failure: bool,
}

fn default_true() -> bool {
    true
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
    /// Full result output (truncated to 50KB at source).
    pub result: Option<String>,
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
    TaskCreated {
        task: Task,
    },
    SubtaskAssigned {
        subtask_id: TaskId,
        worker_id: WorkerId,
    },
    WorkerStarted {
        subtask_id: TaskId,
        worker_id: WorkerId,
    },
    ToolInvoked {
        subtask_id: TaskId,
        tool_name: String,
        tool_input: String,
    },
    ToolResult {
        subtask_id: TaskId,
        tool_output: String,
        exit_code: i32,
    },
    FileModified {
        subtask_id: TaskId,
        file_path: String,
        diff: String,
    },
    SubtaskCompleted {
        result: SubtaskResult,
    },
    SubtaskFailed {
        subtask_id: TaskId,
        error: String,
        recoverable: bool,
        /// Last N lines of stderr from the failed subtask (for diagnostics).
        stderr_tail: String,
        /// Recent tool call names before failure, JSON-serialized array of strings.
        recent_tools: String,
    },
    CheckpointCreated {
        task_id: TaskId,
        snapshot_id: String,
    },
    EditIntent {
        worker_id: WorkerId,
        file_path: String,
        regions: Vec<(u32, u32)>,
    },
    ConflictDetected {
        file_path: String,
        workers: Vec<WorkerId>,
    },
}

// ── File Browser types (for dashboard ListDir/GetFile) ────

/// A directory entry (file or subdirectory).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DirEntry {
    pub name: String,
    pub path: String,
    /// "file" or "directory"
    pub entry_type: String,
    pub size: u64,
}

/// Result of listing a directory in a repo.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DirListing {
    pub repo_id: String,
    pub path: String,
    pub entries: Vec<DirEntry>,
}

/// Content of a single file from a repo.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileContent {
    pub repo_id: String,
    pub path: String,
    pub binary: bool,
    pub size: u64,
    pub content: Option<String>,
    pub language: Option<String>,
    pub truncated: bool,
    pub lines: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subtask_without_steps_field_deserializes_empty() {
        // Backward compat: a Subtask serialized before `steps` existed has no
        // `steps` key. `#[serde(default)]` must yield an empty vec.
        let json = r#"{
            "id": "T1",
            "parent_id": "T0",
            "description": "legacy subtask",
            "status": "Pending",
            "assigned_worker": null,
            "depends_on": [],
            "file_constraints": [],
            "expected_output": "",
            "result": null
        }"#;
        let st: Subtask = serde_json::from_str(json).unwrap();
        assert!(st.steps.is_empty());
        assert_eq!(st.dispatch_mode, DispatchMode::PreferRemote);
    }

    #[test]
    fn workflow_step_round_trips() {
        let step = WorkflowStep {
            agent: "codex".to_string(),
            prompt: "CR: {{prev_summary}}".to_string(),
            agent_config_json: Some(r#"{"agent_name":"reviewer"}"#.to_string()),
            abort_on_failure: false,
        };
        let json = serde_json::to_string(&step).unwrap();
        let back: WorkflowStep = serde_json::from_str(&json).unwrap();
        assert_eq!(back.agent, "codex");
        assert_eq!(back.prompt, "CR: {{prev_summary}}");
        assert_eq!(
            back.agent_config_json.as_deref(),
            Some(r#"{"agent_name":"reviewer"}"#)
        );
        assert!(!back.abort_on_failure);
    }

    #[test]
    fn workflow_step_default_abort_on_failure_true() {
        // When abort_on_failure is absent, default_true kicks in.
        let json = r#"{"agent":"claude-code","prompt":"write"}"#;
        let step: WorkflowStep = serde_json::from_str(json).unwrap();
        assert!(step.abort_on_failure);
    }

    #[test]
    fn subtask_with_steps_serializes_and_round_trips() {
        let subtask = Subtask {
            id: TaskId("st-1".to_string()),
            parent_id: TaskId("t-1".to_string()),
            description: "implement X".to_string(),
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: DispatchMode::PreferRemote,
            dispatch_retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: vec![
                WorkflowStep {
                    agent: "claude-code".to_string(),
                    prompt: "write".to_string(),
                    agent_config_json: None,
                    abort_on_failure: true,
                },
                WorkflowStep {
                    agent: "codex".to_string(),
                    prompt: "CR {{prev_summary}}".to_string(),
                    agent_config_json: None,
                    abort_on_failure: true,
                },
            ],
        };
        let json = serde_json::to_string(&subtask).unwrap();
        // steps appear in the serialized form (proves the field is emitted).
        assert!(json.contains("\"steps\""));
        assert!(json.contains("claude-code"));
        assert!(json.contains("codex"));
        let back: Subtask = serde_json::from_str(&json).unwrap();
        assert_eq!(back.steps.len(), 2);
        assert_eq!(back.steps[0].agent, "claude-code");
        assert_eq!(back.steps[1].agent, "codex");
    }
}
