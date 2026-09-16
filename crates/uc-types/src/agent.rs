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
    /// Must execute on a remote worker via NATS. Revert to Pending on
    /// NATS failure with retry_count increment; mark Failed after 3 retries.
    Remote,
    /// Prefer remote dispatch; fall back to Pending on NATS failure (default).
    #[default]
    PreferRemote,
}

/// Effect class of a node/subtask — governs local-execution eligibility when
/// the remote transport (NatsExecutor) is unavailable (D4 #633 Q2). Serialized
/// snake_case to match the `graph_nodes.effect_class` column values.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum EffectClass {
    /// Pure read (no side effects) — always eligible for local execution.
    ReadOnly,
    /// Tool-class effect with no worktree/CLI dependency — eligible for local
    /// execution when NatsExecutor is unavailable.
    LocalSafe,
    /// Requires a remote worker (coding work: worktree/CLI dependency) —
    /// never executes locally; stays READY with an alert on transport loss.
    #[default]
    RequiresWorker,
}

impl EffectClass {
    /// Column/wire token (`graph_nodes.effect_class` values).
    pub fn as_str(&self) -> &'static str {
        match self {
            EffectClass::ReadOnly => "read_only",
            EffectClass::LocalSafe => "local_safe",
            EffectClass::RequiresWorker => "requires_worker",
        }
    }
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
    /// How this subtask should be dispatched (remote / prefer-remote).
    #[serde(default)]
    pub dispatch_mode: DispatchMode,
    /// Effect class governing local-execution eligibility on transport loss
    /// (D4 #633 Q2). Defaults to `RequiresWorker` (legacy rows never ran
    /// locally via the gateway).
    #[serde(default)]
    pub effect_class: EffectClass,
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
    /// `{{step<N>.summary}}`). Typical chain: grok-build write → codex CR →
    /// grok-build revise.
    #[serde(default)]
    pub steps: Vec<WorkflowStep>,
    /// How many times execution has been retried (for failed-then-retried
    /// subtasks). Distinct from `dispatch_retry_count` (dispatch-level).
    /// Populated by the worker and surfaced to the TUI for retry×N display.
    #[serde(default)]
    pub retry_count: u32,
}

/// A single step in a subtask's multi-agent workflow.
///
/// Each step runs one coding agent (grok-build / claude-code / codex) with a prompt
/// template. Steps run sequentially; the previous step's AgentOutput is
/// available to the next step's prompt via template variables.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct WorkflowStep {
    /// Agent adapter name ("grok-build"/"grok" | "claude-code" | "codex").
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
    /// Number of times to retry this step on failure (0 = no retry, default).
    #[serde(default)]
    pub retry_count: u32,
    /// Delay in ms between retry attempts (0 = retry immediately).
    #[serde(default)]
    pub retry_delay_ms: u64,
    /// Optional condition expression. Evaluated against prior step outputs
    /// before running this step; step is skipped if false. Empty = always run.
    #[serde(default)]
    pub condition: Option<String>,
    /// Optional parallel group. Steps sharing a non-empty group run concurrently.
    /// Steps in a parallel_group MUST be read-only (disallowed_tools includes
    /// Edit, Write, Bash) or the subtask fails. Empty = sequential.
    #[serde(default)]
    pub parallel_group: Option<String>,
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
    /// Token/cost usage reported by the executor (T15 #660, D13 #657).
    ///
    /// Additive and optional: **absent means "not reported", which is not the
    /// same as zero**. Older publishers omit the key entirely, so this must
    /// stay `serde(default)` and must not be serialized when absent — the
    /// gateway tells "no usage" from "zero usage" by whether the field is
    /// present, and any aggregate over `cost`/`tokens` has to keep reporting
    /// "samples reported / samples total" rather than substituting 0.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<SubtaskUsage>,
    /// Review verdict, when this result came from a review node (T16 #661).
    ///
    /// Additive and optional for the same reason as `usage`: nothing produced
    /// a verdict between T6's deletion and now. **Absent means "not
    /// reviewed", which is not the same as "reviewed and approved"** — the UI
    /// must render no verdict line when the key is missing rather than
    /// defaulting `approved` to `false`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub review: Option<SubtaskReview>,
}

/// Token / cost usage attached to a subtask result (T15 #660, D13 #657).
///
/// Every field is optional on purpose: adapters report different subsets (a
/// CLI adapter may report tokens but no cost; others report neither), and a
/// missing field means "unknown" rather than "zero".
///
/// This type doubles as the **wire** shape of the usage block carried on
/// `uc.task.update` — the Python side mirrors the field names verbatim, so
/// renaming anything here is a cross-language contract change.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SubtaskUsage {
    /// Prompt/input tokens consumed, if the adapter reported them.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_tokens: Option<u64>,
    /// Completion/output tokens produced, if the adapter reported them.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_tokens: Option<u64>,
    /// Total cost in USD, if the adapter reported it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total_cost_usd: Option<f64>,
    /// Which adapter (hence which key/account) produced this usage —
    /// `claude_code` / `grok` / … Set only where the adapter is statically
    /// known at the parse site; when unknown it stays `None` and the gateway
    /// omits `usage_source` instead of guessing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

impl SubtaskUsage {
    /// `true` when the block carries no usable number at all.
    ///
    /// A struct that exists but holds nothing must **not** be reported as a
    /// real measurement — the gateway uses this to choose between
    /// `usage_reported: true` and `false`.
    pub fn is_empty(&self) -> bool {
        self.input_tokens.is_none() && self.output_tokens.is_none() && self.total_cost_usd.is_none()
    }

    /// Sum of input+output tokens, or `None` when **both** sides are missing.
    ///
    /// `execution_events.tokens` is a single `BIGINT`, so the two values must
    /// collapse here. A missing side contributes 0 to an otherwise real sum
    /// (an adapter reporting only output tokens still has a real total), but
    /// two missing sides stay `None`: writing 0 would be indistinguishable
    /// from "this run genuinely used no tokens" (D13's hard requirement).
    pub fn total_tokens(&self) -> Option<i64> {
        match (self.input_tokens, self.output_tokens) {
            (None, None) => None,
            (input, output) => Some((input.unwrap_or(0) + output.unwrap_or(0)) as i64),
        }
    }
}

/// One workflow step's usage, as recorded on the terminal event's
/// `payload.steps[]` (T18 #668, ruling #666 C).
///
/// Additive by construction — no new column, no new event type, and
/// `contract_version` does not move. It exists because the executor forwards
/// ONE step's usage on every one of its return paths, so a multi-step chain's
/// node-level `cost`/`tokens` only ever held the **last** step's numbers while
/// the event still declared `usage_reported: true`: a systematically low
/// reading that no coverage mechanism could detect, because the node *did*
/// report something.
///
/// This type doubles as the **wire** shape carried on `uc.task.update` inside
/// the subtask entry's `steps` array — the Python side mirrors the field names
/// verbatim, so renaming anything here is a cross-language contract change
/// (`serde` ignores unknown keys, which makes a one-sided rename a silent drop
/// rather than an error).
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct StepUsage {
    /// Position of the executed unit within the subtask's execution sequence.
    ///
    /// Workflow form: the index into the subtask's declared steps. Legacy
    /// single-agent form (no declared steps): always 0 — that one execution is
    /// the subtask's only unit. A *gap* means that step was skipped (its
    /// condition was false); a skipped step produces no entry at all, so a gap
    /// is the encoding of "did not run" rather than lost data.
    #[serde(default)]
    pub step_index: u32,
    /// The step's parallel group, verbatim from the orchestration. Empty for a
    /// sequential step — the same encoding the step definitions themselves use,
    /// so the two representations agree without a translation table.
    #[serde(default)]
    pub parallel_group: String,
    /// The step's usage, or `None` when its adapter reported nothing.
    ///
    /// `None` means "unknown", never zero (D13 #657). It serializes as an
    /// explicit `null` — deliberately **not** `skip_serializing_if`, unlike
    /// every field of [`SubtaskUsage`]: this is a positional element of an
    /// array, where an explicit null keeps `steps[i].usage` indexable without
    /// an existence check. The two disciplines cannot collide because the
    /// payload is written once into `execution_events.payload` and never parsed
    /// back into this type.
    #[serde(default)]
    pub usage: Option<SubtaskUsage>,
    /// Which adapter ran this step: the usage block's own `source` when it
    /// reported one, else the step's declared agent; `None` when neither is
    /// known.
    ///
    /// It exists so that a step whose adapter reported no numbers still names
    /// who ran it — the only naming left once `usage` is `None`.
    #[serde(default)]
    pub source: Option<String>,
}

/// A review verdict attached to a subtask result (T16 #661, D14 #658).
///
/// Field names mirror the TS `SubtaskDef.review`
/// (`packages/uc-orchestrator/src/orchestrator/orchestrator.ts:110`)
/// **verbatim**: T6 (#642) deleted the TS review pipeline but deliberately kept
/// that field "for the future Rust-side pipeline to repopulate", and the UI
/// already renders it. Renaming anything here is a cross-language contract
/// change that silently blanks the verdict in the TUI.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SubtaskReview {
    /// Whether the reviewer approved the work under review.
    pub approved: bool,
    /// Problems the reviewer found. Absent in older records — the TS side
    /// reads them with `?? []` for exactly that reason.
    #[serde(default)]
    pub issues: Vec<String>,
    /// Non-blocking improvements. Approved reviews can carry these too.
    #[serde(default)]
    pub suggestions: Vec<String>,
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
///
// Subtask-level variants carry the PARENT task_id so downstream
// consumers (WatchTask filters, PyAgentEvent.task_id) can attribute
// events to their task without a store lookup. Mirrors the engine-side
// `AgentEventType`, which has carried both ids from the start.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AgentEventPayload {
    TaskCreated {
        task: Task,
    },
    SubtaskAssigned {
        task_id: TaskId,
        subtask_id: TaskId,
        worker_id: WorkerId,
    },
    WorkerStarted {
        task_id: TaskId,
        subtask_id: TaskId,
        worker_id: WorkerId,
    },
    ToolInvoked {
        task_id: TaskId,
        subtask_id: TaskId,
        tool_name: String,
        tool_input: String,
    },
    ToolResult {
        task_id: TaskId,
        subtask_id: TaskId,
        tool_output: String,
        exit_code: i32,
    },
    FileModified {
        task_id: TaskId,
        subtask_id: TaskId,
        file_path: String,
        diff: String,
    },
    SubtaskCompleted {
        task_id: TaskId,
        result: SubtaskResult,
    },
    SubtaskFailed {
        task_id: TaskId,
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

/// Snapshot of a single subtask's state for checkpoint/recovery.
///
/// Reconstructed by replaying the task's event stream.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct SubtaskSnapshot {
    pub subtask_id: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub result_summary: Option<String>,
}

/// Snapshot of a task's state for checkpoint/recovery.
///
/// Produced by `CheckpointManager::create_snapshot` (latest subtask states +
/// last event offset) and consumed by `recover` (load snapshot, replay events
/// after `last_event_offset`).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TaskSnapshot {
    pub task_id: String,
    pub status: String,
    pub subtasks: Vec<SubtaskSnapshot>,
    pub last_event_offset: u64,
    /// Unix timestamp (milliseconds).
    pub timestamp: i64,
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
        // Legacy rows carry no effect_class — must default to RequiresWorker
        // (never eligible for gateway-local execution).
        assert_eq!(st.effect_class, EffectClass::RequiresWorker);
    }

    #[test]
    fn effect_class_serializes_snake_case() {
        // T5 #641: wire values must match the graph_nodes.effect_class column
        // values ('read_only' / 'local_safe' / 'requires_worker').
        assert_eq!(
            serde_json::to_string(&EffectClass::ReadOnly).unwrap(),
            "\"read_only\""
        );
        assert_eq!(
            serde_json::to_string(&EffectClass::LocalSafe).unwrap(),
            "\"local_safe\""
        );
        assert_eq!(
            serde_json::to_string(&EffectClass::RequiresWorker).unwrap(),
            "\"requires_worker\""
        );
        let back: EffectClass = serde_json::from_str("\"local_safe\"").unwrap();
        assert_eq!(back, EffectClass::LocalSafe);
    }

    #[test]
    fn workflow_step_round_trips() {
        let step = WorkflowStep {
            agent: "codex".to_string(),
            prompt: "CR: {{prev_summary}}".to_string(),
            agent_config_json: Some(r#"{"agent_name":"reviewer"}"#.to_string()),
            abort_on_failure: false,
            retry_count: 0,
            retry_delay_ms: 0,
            condition: None,
            parallel_group: None,
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
            effect_class: EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: vec![
                WorkflowStep {
                    agent: "claude-code".to_string(),
                    prompt: "write".to_string(),
                    agent_config_json: None,
                    abort_on_failure: true,
                    retry_count: 0,
                    retry_delay_ms: 0,
                    condition: None,
                    parallel_group: None,
                },
                WorkflowStep {
                    agent: "codex".to_string(),
                    prompt: "CR {{prev_summary}}".to_string(),
                    agent_config_json: None,
                    abort_on_failure: true,
                    retry_count: 0,
                    retry_delay_ms: 0,
                    condition: None,
                    parallel_group: None,
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

    #[test]
    fn workflow_step_retry_fields_round_trip() {
        let step = WorkflowStep {
            agent: "claude-code".to_string(),
            prompt: "flaky API call".to_string(),
            agent_config_json: None,
            abort_on_failure: true,
            retry_count: 3,
            retry_delay_ms: 5000,
            condition: None,
            parallel_group: None,
        };
        let json = serde_json::to_string(&step).unwrap();
        let back: WorkflowStep = serde_json::from_str(&json).unwrap();
        assert_eq!(back.retry_count, 3);
        assert_eq!(back.retry_delay_ms, 5000);
    }

    #[test]
    fn workflow_step_defaults_retry_zero_when_absent() {
        // Backward compat: a step serialized before retry fields existed.
        let json = r#"{"agent":"codex","prompt":"CR","abort_on_failure":true}"#;
        let step: WorkflowStep = serde_json::from_str(json).unwrap();
        assert_eq!(step.retry_count, 0);
        assert_eq!(step.retry_delay_ms, 0);
    }

    #[test]
    fn workflow_step_condition_round_trip() {
        let step = WorkflowStep {
            agent: "codex".to_string(),
            prompt: "CR".to_string(),
            agent_config_json: None,
            abort_on_failure: true,
            retry_count: 0,
            retry_delay_ms: 0,
            condition: Some("prev.success && prev.files.contains(\"src/\")".to_string()),
            parallel_group: None,
        };
        let json = serde_json::to_string(&step).unwrap();
        let back: WorkflowStep = serde_json::from_str(&json).unwrap();
        assert_eq!(
            back.condition.as_deref(),
            Some("prev.success && prev.files.contains(\"src/\")")
        );
    }

    #[test]
    fn workflow_step_condition_defaults_none_when_absent() {
        // Backward compat: a step serialized before `condition` existed.
        let json = r#"{"agent":"codex","prompt":"CR","abort_on_failure":true}"#;
        let step: WorkflowStep = serde_json::from_str(json).unwrap();
        assert!(step.condition.is_none());
    }

    #[test]
    fn workflow_step_parallel_group_round_trip() {
        let step = WorkflowStep {
            agent: "codex".to_string(),
            prompt: "CR".to_string(),
            agent_config_json: None,
            abort_on_failure: true,
            retry_count: 0,
            retry_delay_ms: 0,
            condition: None,
            parallel_group: Some("review-group".to_string()),
        };
        let json = serde_json::to_string(&step).unwrap();
        let back: WorkflowStep = serde_json::from_str(&json).unwrap();
        assert_eq!(back.parallel_group.as_deref(), Some("review-group"));
    }

    #[test]
    fn workflow_step_parallel_group_defaults_none_when_absent() {
        // Backward compat: a step serialized before `parallel_group` existed.
        let json = r#"{"agent":"codex","prompt":"CR","abort_on_failure":true}"#;
        let step: WorkflowStep = serde_json::from_str(json).unwrap();
        assert!(step.parallel_group.is_none());
    }

    #[test]
    fn subtask_usage_total_tokens_needs_at_least_one_side() {
        // T15 #660 / D13 #657: `execution_events.tokens` is one BIGINT, so the
        // two reported sides collapse here. The rule that matters is the last
        // row — two missing sides stay `None`, because writing 0 would be
        // indistinguishable from "this run genuinely used no tokens".
        let case = |input, output| SubtaskUsage {
            input_tokens: input,
            output_tokens: output,
            ..Default::default()
        };
        assert_eq!(case(None, None).total_tokens(), None, "nothing reported");
        assert_eq!(case(Some(10), None).total_tokens(), Some(10));
        assert_eq!(case(None, Some(4)).total_tokens(), Some(4));
        assert_eq!(case(Some(10), Some(4)).total_tokens(), Some(14));
    }

    #[test]
    fn subtask_usage_is_empty_ignores_source() {
        // A `source` is provenance, not a measurement: a block that names the
        // adapter but carries no number must still read as "not reported", or
        // the gateway would set `usage_reported: true` over two NULL columns.
        let provenance_only = SubtaskUsage {
            source: Some("claude-code".to_string()),
            ..Default::default()
        };
        assert!(provenance_only.is_empty());

        // Any real number flips it, including a token count of exactly 0 —
        // that is a reported zero, which is a measurement.
        assert!(!SubtaskUsage {
            input_tokens: Some(0),
            ..Default::default()
        }
        .is_empty());
        assert!(!SubtaskUsage {
            total_cost_usd: Some(0.0),
            ..Default::default()
        }
        .is_empty());
    }

    #[test]
    fn subtask_result_parses_legacy_payload_without_usage() {
        // Additive change discipline (T15 #660): a publisher built before the
        // field existed omits `usage` entirely. `serde(default)` must accept
        // it, so no `contract_version` bump is needed.
        let json = r#"{
            "subtask_id": "t1",
            "worker_id": "w1",
            "modified_files": [],
            "summary": "ok",
            "success": true,
            "completed_at": "2026-09-15T00:00:00Z",
            "result": null
        }"#;
        let back: SubtaskResult = serde_json::from_str(json).unwrap();
        assert!(back.usage.is_none());
    }

    #[test]
    fn subtask_result_omits_usage_key_when_absent_and_locks_field_names() {
        // Two halves of the same contract:
        //  1. an unset `usage` must not appear in the serialized form, so a
        //     T15 publisher stays byte-identical to a pre-T15 one;
        //  2. the keys it *does* emit are the cross-language contract the
        //     Python mirror must reproduce verbatim.
        let mut result = SubtaskResult {
            subtask_id: TaskId("t1".to_string()),
            worker_id: WorkerId("w1".to_string()),
            modified_files: Vec::new(),
            summary: "ok".to_string(),
            success: true,
            completed_at: chrono::DateTime::from_timestamp(1_757_894_400, 0).unwrap(),
            result: None,
            usage: None,

            review: None,
        };
        let bare = serde_json::to_string(&result).unwrap();
        assert!(
            !bare.contains("usage"),
            "absent usage must not be emitted, got {bare}"
        );

        result.usage = Some(SubtaskUsage {
            input_tokens: Some(10),
            output_tokens: Some(4),
            total_cost_usd: Some(0.5),
            source: Some("claude-code".to_string()),
        });
        let with = serde_json::to_string(&result).unwrap();
        for key in ["input_tokens", "output_tokens", "total_cost_usd", "source"] {
            assert!(with.contains(key), "usage key `{key}` missing from {with}");
        }
        let back: SubtaskResult = serde_json::from_str(&with).unwrap();
        assert_eq!(back.usage, result.usage);
    }

    // ── StepUsage (T18 #668) ────────────────────────────────────────────

    /// Golden JSON produced by the Python mirror, byte for byte.
    ///
    /// `tests/python/test_nats_worker_helpers.py::
    /// test_task_update_payload_emits_steps_with_rust_field_names` pins the
    /// same literal from the other side. `serde` ignores unknown keys, so a
    /// one-sided rename is a *silent* drop — these two fail together.
    #[test]
    fn step_usage_wire_shape_is_locked() {
        let json = r#"[
            {"step_index": 0, "parallel_group": "",
             "usage": {"input_tokens": 10, "output_tokens": 4, "source": "grok-build"},
             "source": "grok-build"},
            {"step_index": 1, "parallel_group": "", "usage": null, "source": "codex"},
            {"step_index": 2, "parallel_group": "review",
             "usage": {"input_tokens": 7, "source": "codex"}, "source": "codex"}
        ]"#;
        let steps: Vec<StepUsage> = serde_json::from_str(json).unwrap();
        assert_eq!(steps.len(), 3);
        assert_eq!(steps[0].step_index, 0);
        assert_eq!(steps[0].source.as_deref(), Some("grok-build"));
        assert_eq!(steps[0].usage.as_ref().unwrap().input_tokens, Some(10));
        // The silent step stays silent AND stays named.
        assert!(steps[1].usage.is_none());
        assert_eq!(steps[1].source.as_deref(), Some("codex"));
        assert_eq!(steps[2].parallel_group, "review");
    }

    /// An element must round-trip to the same explicit-null shape it was read
    /// from — this is what keeps `steps[i].usage` indexable on both sides.
    #[test]
    fn step_usage_serializes_explicit_nulls_unlike_subtask_usage() {
        let silent = StepUsage {
            step_index: 1,
            parallel_group: String::new(),
            usage: None,
            source: Some("codex".to_string()),
        };
        let json = serde_json::to_string(&silent).unwrap();
        assert!(
            json.contains("\"usage\":null"),
            "a positional record keeps the key with an explicit null, got {json}"
        );
        assert!(json.contains("\"source\":\"codex\""), "got {json}");

        // Contrast: SubtaskUsage is a wire delta and omits absent keys, which
        // is exactly what makes a pre-T15 publisher byte-identical.
        assert_eq!(
            serde_json::to_string(&SubtaskUsage::default()).unwrap(),
            "{}"
        );

        // And the round trip is exact in both directions.
        let back: StepUsage = serde_json::from_str(&json).unwrap();
        assert_eq!(back, silent);
    }

    /// Every field carries `#[serde(default)]`, so a malformed element degrades
    /// to a default instead of failing the WHOLE `uc.task.update`.
    ///
    /// That failure mode is the dangerous one: a deserialization error drops
    /// the entire task snapshot for every subtask in it, not just the bad step.
    #[test]
    fn step_usage_tolerates_partial_and_unknown_keys() {
        let partial: StepUsage = serde_json::from_str(r#"{"step_index": 3}"#).unwrap();
        assert_eq!(partial.step_index, 3);
        assert_eq!(partial.parallel_group, "");
        assert!(partial.usage.is_none() && partial.source.is_none());

        let empty: StepUsage = serde_json::from_str("{}").unwrap();
        assert_eq!(empty, StepUsage::default());

        let extra: StepUsage =
            serde_json::from_str(r#"{"step_index": 1, "something_new": [1, 2]}"#).unwrap();
        assert_eq!(extra.step_index, 1);

        // A null `usage` is the normal silent-step shape, not an error.
        let nulls: StepUsage = serde_json::from_str(r#"{"usage": null, "source": null}"#).unwrap();
        assert!(nulls.usage.is_none() && nulls.source.is_none());
    }
}
