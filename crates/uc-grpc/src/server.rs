//! gRPC server wrapping an EngineApi implementor + TaskService.
//!
//! Accepts proto requests, converts to uc-types, calls the engine,
//! and converts results back to proto responses.
//!
//! TaskService uses an in-memory task store (bridge until full Python
//! Orchestrator integration).
//!
//! When the `messaging` feature is enabled, TaskService can publish
//! task submissions to NATS and subscribe to status updates from the
//! Python Orchestrator. If NATS is unavailable, it gracefully degrades
//! to local (newline-split) task decomposition.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::{broadcast, Mutex, RwLock};
use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::conversions::{
    memory_key_from_proto, proto_status_to_task_status, proto_subtask_status_from_str,
    subtask_status_to_proto, task_snapshot_to_proto, task_status_to_proto,
};
use crate::ultimate_coders::dashboard_service_server::DashboardServiceServer;
use crate::ultimate_coders::engine_service_server::{EngineService, EngineServiceServer};
use crate::ultimate_coders::task_service_server::{TaskService, TaskServiceServer};
use crate::ultimate_coders::worker_service_server::WorkerServiceServer;
use crate::ultimate_coders::*;
use crate::worker_service::WorkerRegistry;

// ── NATS message protocol types ──────────────────────────────

/// NATS subject for task submission (gRPC/Dashboard -> Python).
pub const NATS_SUBJECT_TASK_SUBMIT: &str = "uc.task.submit";

/// NATS subject for task status updates (Python -> gRPC).
pub const NATS_SUBJECT_TASK_UPDATE: &str = "uc.task.update";

/// NATS request subject used by the gateway to recover all task snapshots.
pub const NATS_SUBJECT_TASK_SNAPSHOT_REQUEST: &str = "uc.task.snapshot.request";

/// NATS subject for task events (Python -> gRPC).
pub const NATS_SUBJECT_TASK_EVENT: &str = "uc.task.event";

/// NATS subject for consumer heartbeats (Python -> gRPC).
pub const NATS_SUBJECT_HEARTBEAT: &str = "uc.heartbeat";

/// NATS subject for subtask execution dispatch (Rust -> Worker queue group).
pub const NATS_SUBJECT_SUBTASK_EXECUTE: &str = "uc.subtask.execute";

/// NATS subject for file change events (Worker -> gateway + other workers).
/// The gateway subscribes to incrementally re-index changed files into the
/// shared codebase index.
pub const NATS_SUBJECT_FILE_CHANGED: &str = "uc.file.changed";

/// Payload for `uc.task.submit` messages.
///
/// Published by gRPC server when a task is submitted. The Python NATS
/// consumer subscribes to this subject and calls Orchestrator.submit_task().
///
/// `scheduled` is set to `true` by `NatsSubmitDispatcher` (scheduler-fired
/// tasks) and absent when `false` (real-time gRPC submissions). The Python
/// consumer reads `payload.get("scheduled", False)` — absent means real-time.
/// This drives the night-window exclusive mode: when the night window is
/// active, real-time tasks defer to `_pending_tasks` while scheduled tasks
/// bypass the queue (see scheduler-spec.md §"Orchestrator Night-Window
/// Exclusive Mode").
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskSubmit {
    pub task_id: String,
    pub description: String,
    pub project_id: String,
    /// Whether this submit originated from the scheduler (cron/one-shot fire).
    /// `true` = scheduler-fired (bypasses night-window deferral).
    /// Absent/`false` = real-time gRPC submission (subject to deferral).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub scheduled: Option<bool>,
    /// Optional verification command threaded from `ScheduledTask.verify_command`.
    /// The Python consumer passes it to `Orchestrator.submit_task(verify_command=)`,
    /// which threads it to `aggregate(verify_command=)`. None = no verification.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verify_command: Option<String>,
}

/// Payload for `uc.task.update` messages.
///
/// Published by Python Orchestrator when a task or its subtasks change status.
/// The gRPC server subscribes to this subject and updates the in-memory TaskStore.
/// Complete snapshots also carry `description` and `project_id` sibling fields
/// decoded by `NatsTaskUpdateEnvelope` for restart recovery.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskUpdate {
    /// Deduplication key for at-least-once NATS delivery.
    /// Format: `{task_id}:{event_type}:{subtask_id}:{timestamp_ms}`
    #[serde(default)]
    pub message_id: Option<String>,
    pub task_id: String,
    pub status: String,
    /// `true` for worker-only updates that contain a single subtask result
    /// rather than the complete parent task snapshot.
    #[serde(default)]
    pub partial: bool,
    pub subtasks: Vec<NatsSubtaskUpdate>,
    #[serde(default)]
    pub result: Option<String>,
}

/// Wire envelope for a Python task snapshot.
///
/// `NatsTaskUpdate` remains the small status/subtask payload used by existing
/// callers. Complete snapshots add the task identity context as sibling
/// fields so the gateway can rebuild a task after an in-memory restart. The
/// decoder keeps them optional for backward compatibility with older
/// publishers, but both must be present before an unknown task is rehydrated;
/// partial worker updates must never use them to create a task.
#[cfg(any(feature = "messaging", test))]
#[derive(Debug, Clone, serde::Deserialize)]
struct NatsTaskUpdateEnvelope {
    #[serde(flatten)]
    update: NatsTaskUpdate,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    project_id: Option<String>,
}

/// Response returned by the Python Orchestrator for a snapshot request.
#[cfg(any(feature = "messaging", test))]
#[derive(Debug, Clone, serde::Deserialize)]
struct NatsTaskSnapshotResponse {
    #[serde(default)]
    tasks: Vec<NatsTaskUpdateEnvelope>,
}

/// Subtask update within a `NatsTaskUpdate`.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsSubtaskUpdate {
    pub subtask_id: String,
    pub status: String,
    #[serde(default)]
    pub assigned_worker: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub depends_on: Option<Vec<String>>,
    #[serde(default)]
    pub result: Option<String>,
    /// Token/cost usage reported by the worker (T15 #660).
    ///
    /// Additive and optional: absent means "not reported", **never** "zero".
    /// The type is shared with `uc_types::SubtaskResult` so the wire key names
    /// and the domain field names cannot drift apart — the Python publisher
    /// mirrors them verbatim.
    #[serde(default)]
    pub usage: Option<uc_types::SubtaskUsage>,
    /// Per-step usage reported by the worker (T18 #668).
    ///
    /// The disaggregation of `usage` above: the executor forwards ONE step's
    /// usage, so a multi-step chain's node-level `cost`/`tokens` are the LAST
    /// step's numbers. Additive and optional in exactly the same way —
    /// absent/empty means "no per-step records", never a placeholder entry.
    /// Shares `uc_types::StepUsage` with the event payload's `steps[]` so the
    /// wire keys and the payload keys cannot drift apart.
    #[serde(default)]
    pub steps: Option<Vec<uc_types::StepUsage>>,
    /// Review verdict reported by a reviewer worker (T16 #661).
    ///
    /// Same additive discipline as `usage`: absent means "no verdict", and a
    /// verdict is only ever produced by a node that actually ran a review.
    /// Shares `uc_types::SubtaskReview` with the domain type so the wire keys
    /// (which mirror the TS `SubtaskDef.review`) cannot drift from the field
    /// names the gateway carries.
    #[serde(default)]
    pub review: Option<uc_types::SubtaskReview>,
    /// Attempt number the reporting worker executed under (T4 #640) — the
    /// worker echoes the `retry_count` from the dispatch envelope. Stamped
    /// only on worker-sourced partial updates; `None` = legacy publisher
    /// without the stamp (fencing skipped, upgrade-window compat).
    #[serde(default)]
    pub attempt_id: Option<u64>,
}

/// Convert the wire representation of a subtask into the domain type used by
/// `TaskStore`. This is shared by the rehydration path and the normal upsert
/// path so recovered and live subtasks receive the same defaults.
fn nats_subtask_to_domain(task_id: &str, update: &NatsSubtaskUpdate) -> uc_types::Subtask {
    let status =
        subtask_status_from_str(&update.status).unwrap_or(uc_types::SubtaskStatus::Pending);
    let assigned_worker = update
        .assigned_worker
        .as_ref()
        .map(|worker| uc_types::WorkerId(worker.clone()));
    let result = update
        .result
        .as_ref()
        .map(|summary| uc_types::SubtaskResult {
            subtask_id: uc_types::TaskId(update.subtask_id.clone()),
            worker_id: assigned_worker.clone().unwrap_or_default(),
            modified_files: Vec::new(),
            summary: summary.clone(),
            success: !matches!(&status, uc_types::SubtaskStatus::Failed),
            completed_at: chrono::Utc::now(),
            result: Some(summary.clone()),
            usage: update.usage.clone(),
            review: update.review.clone(),
        });

    uc_types::Subtask {
        id: uc_types::TaskId(update.subtask_id.clone()),
        parent_id: uc_types::TaskId(task_id.to_string()),
        description: update.description.clone().unwrap_or_default(),
        status,
        assigned_worker,
        depends_on: update
            .depends_on
            .clone()
            .unwrap_or_default()
            .into_iter()
            .map(uc_types::TaskId)
            .collect(),
        file_constraints: Vec::new(),
        expected_output: String::new(),
        result,
        dispatch_mode: uc_types::DispatchMode::default(),
        effect_class: uc_types::EffectClass::default(),
        dispatch_retry_count: 0,
        retry_count: 0,
        required_capabilities: Vec::new(),
        agent_config_json: None,
        steps: Vec::new(),
    }
}

/// Payload for `uc.task.event` messages.
///
/// Published by Python Orchestrator for real-time events (tool calls, LLM
/// requests, etc.). The gRPC server pushes these into the TaskStore event
/// log for WatchTask streaming.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskEvent {
    /// Event schema version. Consumers ignore unknown versions.
    #[serde(default = "default_event_version")]
    pub v: u32,
    /// Deduplication key for at-least-once NATS delivery.
    /// Format: `{task_id}:{event_type}:{subtask_id}:{timestamp_ms}`
    #[serde(default)]
    pub message_id: Option<String>,
    pub r#type: String,
    pub task_id: String,
    #[serde(default)]
    pub subtask_id: Option<String>,
    #[serde(default)]
    pub data: serde_json::Map<String, serde_json::Value>,
}

fn default_event_version() -> u32 {
    1
}

/// Payload for `uc.subtask.execute` messages.
///
/// Published by the Rust scheduler when a subtask becomes ready (all
/// dependencies completed).  Workers subscribe to this subject via a
/// NATS queue group so that each subtask is consumed by exactly one worker.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsSubtaskExecute {
    /// Deduplication key for at-least-once NATS delivery.
    #[serde(default)]
    pub message_id: Option<String>,
    // T6 #642 (D2/D3 follow-through): the legacy `task_id`/`subtask_id`
    // identity keys are gone — the graph envelope (`graph_id`/`node_id`) is
    // the single identity source on the wire. Pre-T4 archives carrying the
    // old keys still parse (unknown keys are ignored); their identity was
    // never read after T4 made the envelope authoritative.
    pub description: String,
    #[serde(default)]
    pub expected_output: String,
    #[serde(default)]
    pub file_constraints: Vec<String>,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    /// Retry count — incremented on each re-dispatch after worker failure.
    #[serde(default)]
    pub retry_count: u32,
    /// Dispatch mode — controls routing behavior.
    #[serde(default)]
    pub dispatch_mode: uc_types::DispatchMode,
    /// Effect class — governs local-execution eligibility on transport loss
    /// (T5 #641 / D4 #633 Q2). Additive: legacy workers ignore it.
    #[serde(default)]
    pub effect_class: uc_types::EffectClass,
    /// Capabilities required by this subtask (e.g., "rust", "python", "docker").
    /// Worker must possess ALL listed capabilities to accept this subtask.
    #[serde(default)]
    pub required_capabilities: Vec<String>,
    /// Per-subtask agent configuration overrides (JSON string).
    /// Keys: tools, allowed_tools, disallowed_tools, mcp_configs,
    ///       append_system_prompt, agent_name, agents_json.
    #[serde(default)]
    pub agent_config_json: Option<String>,
    /// Ordered multi-agent workflow steps. Empty = single-agent execution
    /// via agent_config_json (backward compatible). When non-empty, the
    /// worker runs steps in order, threading outputs into next step's prompt.
    #[serde(default)]
    pub steps: Vec<uc_types::WorkflowStep>,
    /// Project scope for cross-repo search and memory sharing.
    #[serde(default)]
    pub project_id: String,
    // ── Execution envelope (T1 #637) ─────────────────────────────
    // Identity of this dispatch in the future durable graph runtime.
    // Transitional identity mapping: graph_id = task_id, node_id =
    // subtask_id, attempt_id = dispatch_retry_count, worker_epoch = "".
    // All additive + `serde(default)`: legacy consumers ignore them.
    /// Graph (task) this execution belongs to.
    #[serde(default)]
    pub graph_id: String,
    /// Node (subtask) being executed.
    #[serde(default)]
    pub node_id: String,
    /// Attempt number within (graph_id, node_id), decimal string.
    #[serde(default)]
    pub attempt_id: String,
    /// Deterministic sha256("{graph}:{node}:{attempt}") hex[:32] — identical
    /// across re-sends of the same dispatch (unlike the millis-based
    /// `message_id`).
    #[serde(default)]
    pub idempotency_key: String,
    /// Worker fencing epoch (empty until T3).
    #[serde(default)]
    pub worker_epoch: String,
    /// Execution contract the publisher speaks. Workers must not accept
    /// dispatches from a gateway they cannot interoperate with (T4 enforces;
    /// T1 only produces).
    #[serde(default)]
    pub contract_version: String,
    /// Gateway-composed dependency context (T10 #652 / D10 #647). Additive —
    /// legacy workers ignore unknown keys; absent when the node has no
    /// dependencies or the graph plane has nothing committed to say.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_block: Option<uc_types::ContextBlock>,
}

fn default_timeout() -> u64 {
    600
}

/// Build the `uc.subtask.execute` dispatch payload for one ready subtask.
///
/// Both gateway publishers (`publish_ready_subtasks` and
/// `dispatch_ready_subtasks`) go through this single constructor so the
/// execution envelope (T1 #637) is emitted identically on every dispatch:
/// identity mapping `graph_id = task_id`, `node_id = subtask_id`,
/// `attempt_id = dispatch_retry_count` (carried as `retry_count` on the
/// wire), `worker_epoch = ""`, plus the deterministic idempotency key and
/// the gateway's `contract_version`. The envelope half of the payload is
/// byte-identical across re-sends of the same dispatch — unlike `message_id`
/// (T4 will key dedup on it).
#[cfg(feature = "messaging")]
fn subtask_execute_payload(
    task_id: &str,
    st: &uc_types::Subtask,
    project_id: &str,
    expected_output: &str,
    file_constraints: &[String],
    context_block: Option<uc_types::ContextBlock>,
) -> NatsSubtaskExecute {
    let mut envelope =
        uc_types::ExecutionEnvelope::for_dispatch(task_id, &st.id.0, st.dispatch_retry_count);
    envelope.context_block = context_block.clone();
    NatsSubtaskExecute {
        message_id: Some(format!(
            "{}:execute:{}:{}",
            task_id,
            st.id.0,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis()
        )),
        // T4 #640 (D3 lockstep) + T6 #642: the graph envelope is the single
        // identity source on the wire — the legacy `task_id`/`subtask_id`
        // keys no longer exist. Workers read `graph_id`/`node_id`.
        description: st.description.clone(),
        expected_output: expected_output.to_string(),
        file_constraints: file_constraints.to_vec(),
        timeout_seconds: 600,
        retry_count: st.dispatch_retry_count,
        dispatch_mode: st.dispatch_mode.clone(),
        effect_class: st.effect_class,
        required_capabilities: st.required_capabilities.clone(),
        agent_config_json: st.agent_config_json.clone(),
        steps: st.steps.clone(),
        project_id: project_id.to_string(),
        graph_id: envelope.graph_id,
        node_id: envelope.node_id,
        attempt_id: envelope.attempt_id,
        idempotency_key: envelope.idempotency_key,
        worker_epoch: envelope.worker_epoch,
        contract_version: envelope.contract_version,
        context_block,
    }
}

/// T10 #652 / D10 #647 — compose the dependency context block for one ready
/// subtask from the graph plane's committed outputs (the single source of
/// truth), before it is handed to the payload builder. Depth-1 deps only;
/// an empty dep set, no graph shadow, or a read failure all degrade to
/// `None` — composing context must never fail (or delay) a dispatch.
#[cfg(feature = "messaging")]
async fn compose_context_block(
    task_store: &Arc<Mutex<TaskStore>>,
    graph_id: &str,
    dep_ids: &[String],
) -> Option<uc_types::ContextBlock> {
    if dep_ids.is_empty() {
        return None;
    }
    let entries = {
        let store = task_store.lock().await;
        // No graph plane → no composed context (never a failed dispatch).
        let sink = store.graph_shadow()?;
        sink.committed_dep_outputs(graph_id, dep_ids).await
    };
    uc_types::ContextBlock::compose(entries)
}

/// T12 #654 / D12 #649 — hosts already running this task's other nodes.
///
/// The locality dimension of the placement score prefers a worker that
/// shares a host with a sibling node's worker (same checkout, warm caches,
/// no cross-host churn). Only workers assigned to *this* task count, and
/// only hosts the registry actually knows (a worker that sent no
/// `hostname` in its registration metadata contributes nothing).
///
/// Takes the already-locked store + registry: `TaskStore` is behind a
/// `tokio::sync::Mutex`, so re-locking here would deadlock.
#[cfg(feature = "messaging")]
fn sibling_worker_hosts(
    store: &TaskStore,
    registry: &crate::worker_service::WorkerRegistry,
    task_id: &str,
) -> std::collections::HashSet<String> {
    let mut hosts = std::collections::HashSet::new();
    if let Some(task) = store.get_task(task_id) {
        for st in &task.subtasks {
            if let Some(worker_id) = &st.assigned_worker {
                if let Some(host) = registry.worker_host(&worker_id.0) {
                    hosts.insert(host);
                }
            }
        }
    }
    hosts
}

/// T12 #654 / D12 #649 — the subject a ready node is published to.
///
/// Affinity placement returns the target worker's per-worker subject when a
/// candidate clears the threshold; otherwise (no overlap, no declared
/// per-worker topic, nothing available, everything stale) the node goes to
/// the shared subject. Placement is a **soft preference**: a `None` score is
/// a normal outcome and never a dispatch failure — every node stays
/// dispatchable through the shared overflow.
#[cfg(feature = "messaging")]
fn resolve_dispatch_subject(
    registry: &crate::worker_service::WorkerRegistry,
    subtask: &uc_types::Subtask,
    project_id: &str,
    sibling_hosts: &std::collections::HashSet<String>,
) -> String {
    match registry.placement_target(
        &subtask.required_capabilities,
        project_id,
        &subtask.file_constraints,
        sibling_hosts,
    ) {
        Some(placement) => {
            tracing::info!(
                subtask_id = %subtask.id.0,
                target_worker = %placement.worker_id,
                subject = %placement.subject,
                affinity_hits = placement.affinity_hits,
                load_percent = placement.load_percent,
                same_host = placement.same_host,
                "Affinity placement: targeting a worker's per-worker subject (T12 #654)"
            );
            placement.subject
        }
        None => NATS_SUBJECT_SUBTASK_EXECUTE.to_string(),
    }
}

/// JetStream dedup headers for one dispatch (T4 #640).
///
/// `Nats-Msg-Id` is set to the deterministic `idempotency_key`, so
/// JetStream's `duplicate_window` (120s on `UC_SUBTASKS`) collapses re-sends
/// of the same dispatch into a single stored message — a re-dispatch caused
/// by a retry, a re-publish, or a gateway restart no longer reaches a worker
/// twice. The key is stable across re-sends by construction; `message_id` is
/// not (it carries a millis stamp), which is exactly why it can never be the
/// dedup key.
///
/// Measured against a live stream during T4 research: the header is honoured
/// on plain `Client::publish` as well as on `jetstream.publish` — dedup is
/// stream-side, not publisher-side — so the gateway keeps its core-NATS
/// publish path and only gains a header.
#[cfg(feature = "messaging")]
fn dispatch_dedup_headers(idempotency_key: &str) -> async_nats::HeaderMap {
    let mut headers = async_nats::HeaderMap::new();
    headers.insert("Nats-Msg-Id", idempotency_key);
    headers
}

/// Payload for `uc.heartbeat` messages.
///
/// Published periodically by the Python NATS consumer. The gRPC server
/// monitors heartbeats and marks tasks as Failed if no heartbeat is
/// received within the configured timeout.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsHeartbeat {
    pub consumer_id: String,
    pub timestamp: String,
    /// Worker's cumulative `stale_dispatch_dropped` counter (T4 #640 / D7):
    /// old-envelope dispatches the worker term-dropped. Flat field on the
    /// heartbeat payload; `None` = legacy worker without it.
    #[serde(default)]
    pub stale_dispatch_dropped: Option<u64>,
}

/// Payload for `uc.file.changed` messages.
///
/// Workers broadcast this when they modify a file. The gateway uses the
/// embedded `content` to incrementally re-index the file into the shared
/// codebase index (text + AST + semantic) without needing filesystem access
/// to the worker's worktree.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct NatsFileChanged {
    pub repo_id: String,
    pub file_path: String,
    /// New full file content (UTF-8). Empty for deletes.
    #[serde(default)]
    pub content: String,
    /// "created" | "modified" | "deleted" | "renamed"
    #[serde(default)]
    pub change_type: String,
}

// ── Helper: parse status from NATS message strings ───────────

/// Parse a TaskStatus from its string representation.
///
/// Returns None if the string does not match any known status.
fn task_status_from_str(s: &str) -> Option<uc_types::TaskStatus> {
    match s.to_lowercase().as_str() {
        "created" => Some(uc_types::TaskStatus::Created),
        "planning" => Some(uc_types::TaskStatus::Planning),
        "in_progress" | "inprogress" => Some(uc_types::TaskStatus::InProgress),
        "completed" => Some(uc_types::TaskStatus::Completed),
        "failed" => Some(uc_types::TaskStatus::Failed),
        "paused" => Some(uc_types::TaskStatus::Paused),
        _ => None,
    }
}

/// Parse a SubtaskStatus from its string representation.
///
/// Returns None if the string does not match any known status.
fn subtask_status_from_str(s: &str) -> Option<uc_types::SubtaskStatus> {
    // ponytail: Python sends lowercase ("assigned", "in_progress"), Rust uses PascalCase
    match s.to_lowercase().as_str() {
        "pending" => Some(uc_types::SubtaskStatus::Pending),
        "assigned" => Some(uc_types::SubtaskStatus::Assigned),
        "in_progress" | "inprogress" => Some(uc_types::SubtaskStatus::InProgress),
        "completed" => Some(uc_types::SubtaskStatus::Completed),
        "failed" => Some(uc_types::SubtaskStatus::Failed),
        "conflicted" => Some(uc_types::SubtaskStatus::Conflicted),
        _ => None,
    }
}

/// Extract a boolean value from a JSON map entry.
///
/// Handles both boolean values (`true`/`false`) and string values
/// (`"true"`/`"false"`). Python publishers send booleans as JSON `true`/`false`,
/// but some sources may send them as strings. Returns `default` if the key
/// is missing or neither a bool nor a string.
#[cfg(feature = "messaging")]
fn json_bool_or_default(
    data: &serde_json::Map<String, serde_json::Value>,
    key: &str,
    default: bool,
) -> bool {
    match data.get(key) {
        Some(serde_json::Value::Bool(b)) => *b,
        Some(serde_json::Value::String(s)) => s == "true",
        _ => default,
    }
}

// ── In-memory task store ─────────────────────────────────────

/// In-memory store for tasks and events, used by TaskService.
///
/// When NATS is available, the store is updated by the Python Orchestrator
/// via `apply_update()`. Task submission is insert-only on the Rust side:
/// decomposition belongs to the TS planner / Python orchestrator (D2; the
/// newline-split fork was removed in T5 #641 — NATS availability must not
/// change WHAT).
///
/// Events are recorded via `Arc<dyn EventStore>` (unified with uc-engine's
/// EventStore trait). WatchTask streams replay from EventStore then switch
/// to the broadcast channel for real-time delivery.
pub struct TaskStore {
    tasks: HashMap<String, uc_types::Task>,
    /// Inline event log — kept for backward compatibility with existing callers.
    /// New code should use `event_store` for reads.
    events: Vec<uc_engine::AgentEventType>,
    /// Unified EventStore — the single source of truth for event persistence.
    event_store: Arc<dyn uc_engine::EventStore>,
    /// Optional async backend for task persistence (PostgreSQL, etc.).
    /// Write-ahead: fire-and-forget upsert on every mutation (HashMap stays
    /// the read source of truth). Startup recovery via `load_tasks_from_backend`
    /// reloads the HashMap from PG; runtime reads never hit PG directly.
    task_backend: Option<Arc<dyn uc_engine::TaskStoreBackend>>,
    /// Optional graph-table shadow writer (T2, `UC_GRAPH_SHADOW=on` only).
    /// When wired at the assembly point, every `persist_task` also upserts
    /// the graph row tables fire-and-forget, warn-only. `None` (default)
    /// means zero behavior change. Object-safe trait so this crate needs no
    /// `storage` feature of its own.
    graph_shadow: Option<Arc<dyn uc_engine::GraphShadowSink>>,
    /// Last heartbeat timestamp from Python NATS consumer.
    last_heartbeat: Option<chrono::DateTime<chrono::Utc>>,
    /// Per-Worker heartbeat timestamps (worker_id -> last seen).
    /// Used for distributed Worker failure detection.
    worker_heartbeats: HashMap<String, chrono::DateTime<chrono::Utc>>,
    /// Per-subtask "Assigned at" timestamps (subtask_id -> when marked Assigned).
    /// Used to revert Assigned subtasks that no worker ever picked up (e.g.
    /// queue group had no subscribers, or all workers were too busy) back to
    /// Pending so they can be re-dispatched. Without this they stuck Assigned
    /// forever (get_ready_subtasks only returns Pending).
    assigned_subtask_times: HashMap<String, chrono::DateTime<chrono::Utc>>,
    /// Deduplication map for NATS at-least-once delivery.
    /// Keys are message_id strings; values are insertion timestamps.
    /// Entries older than 5 minutes are purged on each check.
    seen_messages: HashMap<String, Instant>,
    /// Late-result fencing counter (T4 #640): worker results rejected because
    /// their stamped attempt is older than the subtask's current attempt
    /// (the attempt was fenced and re-dispatched). Surfaced via the getter +
    /// a `stale_result_rejected` task event per rejection.
    stale_dispatch_dropped: u64,
    /// Per-worker cumulative `stale_dispatch_dropped` reported via heartbeat
    /// (T4 #640 / D7): the worker terms old-envelope dispatches and reports
    /// its running total here. Keyed by consumer_id.
    worker_stale_dispatch_dropped: HashMap<String, u64>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskStore {
    pub fn new() -> Self {
        Self {
            tasks: HashMap::new(),
            events: Vec::new(),
            event_store: Arc::new(uc_engine::InMemoryEventStore::new()),
            task_backend: None,
            graph_shadow: None,
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            assigned_subtask_times: HashMap::new(),
            seen_messages: HashMap::new(),
            stale_dispatch_dropped: 0,
            worker_stale_dispatch_dropped: HashMap::new(),
        }
    }

    /// Create with a specific EventStore backend.
    pub fn with_event_store(event_store: Arc<dyn uc_engine::EventStore>) -> Self {
        Self {
            tasks: HashMap::new(),
            events: Vec::new(),
            event_store,
            task_backend: None,
            graph_shadow: None,
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            assigned_subtask_times: HashMap::new(),
            seen_messages: HashMap::new(),
            stale_dispatch_dropped: 0,
            worker_stale_dispatch_dropped: HashMap::new(),
        }
    }

    /// Create with both a TaskStoreBackend and an EventStore.
    pub fn with_backend(
        task_backend: Arc<dyn uc_engine::TaskStoreBackend>,
        event_store: Arc<dyn uc_engine::EventStore>,
    ) -> Self {
        Self {
            tasks: HashMap::new(),
            events: Vec::new(),
            event_store,
            task_backend: Some(task_backend),
            graph_shadow: None,
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            assigned_subtask_times: HashMap::new(),
            seen_messages: HashMap::new(),
            stale_dispatch_dropped: 0,
            worker_stale_dispatch_dropped: HashMap::new(),
        }
    }

    /// Dedup TTL: messages older than this are considered expired.
    const DEDUP_TTL: std::time::Duration = std::time::Duration::from_secs(300); // 5 minutes

    /// Maximum seen_messages entries before triggering a purge.
    const DEDUP_MAX_ENTRIES: usize = 10_000;

    /// Cap on the inline event log (`self.events`). Without this, a long-running
    /// server accumulates events unbounded → OOM → crash → session interruption.
    /// The persistent EventStore remains the source of truth for full history;
    /// the inline log only serves recent replay + broadcast diffs, so capping
    /// the tail is safe.
    const INLINE_EVENTS_MAX: usize = 5_000;

    /// Cap on retained tasks in the in-memory HashMap. Terminal tasks beyond
    /// this count are evicted to prevent unbounded growth on a long-running
    /// server (OOM → crash → session interruption). Larger than the Python
    /// worker's cap because this is the shared server-side store queried by the
    /// Dashboard for recent history; older tasks live in the persistent backend.
    const MAX_RETAINED_TASKS: usize = 1_000;

    /// Evict the oldest terminal (Completed/Failed/Cancelled) tasks when the
    /// in-memory map exceeds MAX_RETAINED_TASKS. Non-terminal tasks are never
    /// evicted (they may still be executing). Returns the count evicted.
    pub fn evict_completed_tasks(&mut self) -> usize {
        if self.tasks.len() <= Self::MAX_RETAINED_TASKS {
            return 0;
        }
        // Collect terminal tasks with their updated_at as the eviction ordering.
        let mut terminal: Vec<(String, chrono::DateTime<chrono::Utc>)> = self
            .tasks
            .iter()
            .filter(|(_, t)| {
                matches!(
                    t.status,
                    uc_types::TaskStatus::Completed | uc_types::TaskStatus::Failed
                )
            })
            .map(|(id, t)| (id.clone(), t.updated_at))
            .collect();
        if terminal.is_empty() {
            return 0;
        }
        // Evict oldest terminal tasks first (smallest updated_at).
        terminal.sort_by_key(|(_, ts)| *ts);
        let excess = self.tasks.len() - Self::MAX_RETAINED_TASKS;
        let to_evict = std::cmp::min(excess, terminal.len());
        for (id, _) in terminal.iter().take(to_evict) {
            // Defensive: clear any residual assigned-at tracking for this task's
            // subtasks. Terminal tasks' subtasks should already be non-Assigned
            // (hence cleared on transition), but this guards against future paths
            // that remove a task without going through update_subtask_status.
            if let Some(task) = self.tasks.get(id) {
                for st in &task.subtasks {
                    self.assigned_subtask_times.remove(&st.id.0);
                }
            }
            self.tasks.remove(id);
        }
        to_evict
    }

    /// Check if a message_id has already been processed.
    /// Returns `true` if the message is a duplicate (already seen).
    /// If not a duplicate, records the message_id and returns `false`.
    pub fn check_and_record_message_id(&mut self, message_id: &Option<String>) -> bool {
        // No message_id means no dedup — always process
        let mid = match message_id {
            Some(id) if !id.is_empty() => id,
            _ => return false,
        };

        if self.seen_messages.contains_key(mid) {
            tracing::debug!(message_id = %mid, "Skipping duplicate NATS message");
            return true;
        }

        self.seen_messages.insert(mid.clone(), Instant::now());

        // Purge expired entries if the map is getting large
        if self.seen_messages.len() > Self::DEDUP_MAX_ENTRIES {
            self.purge_expired_dedup_entries();
        }

        false
    }

    /// Remove entries older than DEDUP_TTL from the seen_messages map.
    fn purge_expired_dedup_entries(&mut self) {
        let now = Instant::now();
        self.seen_messages
            .retain(|_, instant| now.duration_since(*instant) < Self::DEDUP_TTL);
    }

    /// Record an event: push to inline log AND append to EventStore.
    /// Returns the proto TaskEvent for callers to broadcast (avoids re-reading
    /// from self.events, which is unreliable when the log is at capacity —
    /// record_event drains oldest entries, shifting indices).
    fn record_event_with_subject(
        &mut self,
        event: uc_engine::AgentEventType,
        subject: &str,
    ) -> TaskEvent {
        let proto: TaskEvent = event.clone().into();
        // Inline log (legacy, for tests and immediate reads)
        self.events.push(event.clone());
        // Cap the inline log to prevent unbounded growth (OOM on long runs).
        // Keep the most recent events; full history lives in the EventStore.
        if self.events.len() > Self::INLINE_EVENTS_MAX {
            let drop_n = self.events.len() - Self::INLINE_EVENTS_MAX;
            self.events.drain(0..drop_n);
        }
        // EventStore (unified, persistent source of truth)
        // ponytail: spawn is fire-and-forget; if no runtime, skip (tests)
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            let es = self.event_store.clone();
            let subj = subject.to_string();
            handle.spawn(async move {
                let _ = es.append(&subj, &event).await;
            });
        }
        proto
    }

    /// Fire-and-forget upsert to the task backend via `update_task`.
    ///
    /// Mirrors the `record_event_with_subject` spawn pattern: spawns the
    /// async backend call on the current tokio runtime, logging failures via
    /// `tracing::warn`. No-op when `task_backend` is `None` or no runtime is
    /// running (e.g. sync unit tests). The HashMap remains the read source of
    /// truth; PG is write-ahead for restart recovery (`load_tasks_from_backend`
    /// reloads it on startup).
    ///
    /// T2 shadow: when a graph sink is wired (only with `UC_GRAPH_SHADOW=on`
    /// at the assembly point), the same task is ALSO upserted into the graph
    /// row tables, fire-and-forget and warn-only. The sink never feeds back
    /// into this path — read behavior is untouched.
    fn persist_task(&self, task: &uc_types::Task) {
        if let Some(backend) = &self.task_backend {
            if let Ok(handle) = tokio::runtime::Handle::try_current() {
                let backend = backend.clone();
                let task = task.clone();
                handle.spawn(async move {
                    if let Err(e) = backend.update_task(task).await {
                        tracing::warn!("task_backend update_task failed: {}", e);
                    }
                });
            }
        }
        if let Some(sink) = &self.graph_shadow {
            if let Ok(handle) = tokio::runtime::Handle::try_current() {
                let sink = sink.clone();
                let task = task.clone();
                handle.spawn(async move {
                    sink.shadow_persist(&task).await;
                });
            }
        }
    }

    /// Inject the graph-table shadow writer (startup assembly only, gated by
    /// `UC_GRAPH_SHADOW=on`). Until set, `persist_task` has no graph-path
    /// cost at all.
    pub fn set_graph_shadow(&mut self, sink: Arc<dyn uc_engine::GraphShadowSink>) {
        self.graph_shadow = Some(sink);
    }

    /// Clone of the graph shadow sink, if wired. The heartbeat monitor uses
    /// this as its storage-free handle to the graph rows for the timeout
    /// sweep (T6 #642).
    pub fn graph_shadow(&self) -> Option<Arc<dyn uc_engine::GraphShadowSink>> {
        self.graph_shadow.clone()
    }

    // ── T3 (#639) graph-plane verb fan-out ───────────────────────────
    //
    // Fire-and-forget, `None` zero-overhead: when no sink is wired the
    // helpers return before touching anything, and the tokio-runtime guard
    // makes sync callers/tests no-ops. Sinks can never fail the legacy
    // path — verb trait defaults are no-ops and the GraphStore impls are
    // warn-only internally. Identity rides the T1 `ExecutionEnvelope`
    // (transitional mapping: `graph_id = task_id`, `node_id = subtask_id`,
    // `attempt_id = dispatch_retry_count`).

    fn graph_verb<F, Fut>(&self, make: F)
    where
        F: FnOnce(Arc<dyn uc_engine::GraphShadowSink>) -> Fut + Send + 'static,
        Fut: std::future::Future<Output = ()> + Send + 'static,
    {
        let Some(sink) = &self.graph_shadow else {
            return;
        };
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            let sink = sink.clone();
            handle.spawn(async move {
                make(sink).await;
            });
        }
    }

    /// Publish/dispatch marked the subtask `Assigned` → graph plane
    /// `READY → SCHEDULED → RUNNING` + fresh attempt.
    fn fanout_graph_schedule(
        &self,
        graph_id: &str,
        node_id: &str,
        attempt: u32,
        worker_id: Option<String>,
    ) {
        let (graph_id, node_id) = (graph_id.to_string(), node_id.to_string());
        self.graph_verb(move |sink| async move {
            let env = uc_types::ExecutionEnvelope::new(&graph_id, &node_id, &attempt.to_string());
            sink.on_schedule(&env, worker_id.as_deref()).await;
        });
    }

    /// A worker reported the subtask running → refresh the attempt heartbeat
    /// (the datum the graph plane's `timeout_sweep` judges staleness by).
    fn fanout_graph_heartbeat(&self, graph_id: &str, node_id: &str, attempt: u32) {
        let (graph_id, node_id) = (graph_id.to_string(), node_id.to_string());
        self.graph_verb(move |sink| async move {
            let env = uc_types::ExecutionEnvelope::new(&graph_id, &node_id, &attempt.to_string());
            sink.on_heartbeat(&env).await;
        });
    }

    /// Terminal-success derivation → commit-once for the node.
    ///
    /// `usage` (T15 #660) and `steps` (T18 #668) both ride down to `on_commit`:
    /// the first binds the terminal event's `cost`/`tokens`, the second is the
    /// disaggregation of that same report. Dropping either at this hop is
    /// invisible — the commit still succeeds, only the event quietly loses the
    /// data.
    fn fanout_graph_commit(
        &self,
        graph_id: &str,
        node_id: &str,
        attempt: u32,
        result_ref: Option<String>,
        usage: Option<uc_types::SubtaskUsage>,
        steps: Option<Vec<uc_types::StepUsage>>,
    ) {
        let (graph_id, node_id) = (graph_id.to_string(), node_id.to_string());
        self.graph_verb(move |sink| async move {
            let env = uc_types::ExecutionEnvelope::new(&graph_id, &node_id, &attempt.to_string());
            sink.on_commit(
                &env,
                result_ref.as_deref(),
                usage.as_ref(),
                steps.as_deref(),
            )
            .await;
        });
    }

    /// Terminal-failure derivation or a reaper revoke → attempt FAILED +
    /// fence; the sink re-arms the node to READY while the retry budget
    /// lasts, else fails it.
    fn fanout_graph_fail(&self, graph_id: &str, node_id: &str, attempt: u32, reason: &str) {
        let (graph_id, node_id, reason) = (
            graph_id.to_string(),
            node_id.to_string(),
            reason.to_string(),
        );
        self.graph_verb(move |sink| async move {
            let env = uc_types::ExecutionEnvelope::new(&graph_id, &node_id, &attempt.to_string());
            sink.on_fail(&env, &reason).await;
        });
    }

    /// Fire-and-forget INSERT of a newly-created task to the backend via
    /// `submit_task`.
    ///
    /// Used at the two submit points (`submit_task`, `submit_task_pending`)
    /// and the create-if-not-exists path in `update_task`. The backend's
    /// `update_task` is an upsert (INSERT ON CONFLICT, see task_store.rs), so
    /// `persist_task` alone would suffice — this explicit INSERT is kept so the
    /// first write uses the cheaper single-row INSERT path rather than the
    /// upsert's conflict check.
    fn persist_new_task(&self, task: &uc_types::Task) {
        if let Some(backend) = &self.task_backend {
            if let Ok(handle) = tokio::runtime::Handle::try_current() {
                let backend = backend.clone();
                let task = task.clone();
                handle.spawn(async move {
                    if let Err(e) = backend.submit_task(task).await {
                        tracing::warn!("task_backend submit_task failed: {}", e);
                    }
                });
            }
        }
    }

    /// Load all tasks from the backend into the in-memory HashMap (startup
    /// recovery). No-op when `task_backend` is None. Called once at server
    /// startup before serving, so subsequent sync reads see the recovered state.
    ///
    /// ponytail: one-shot load, not per-read delegation. Single-gateway
    /// deployments (the current architecture — workers scale, gateway doesn't)
    /// stay consistent via the write-path. Multi-gateway live-read consistency
    /// is out of scope (would need per-read backend delegation + async sigs).
    pub async fn load_from_backend(&mut self) -> Result<usize, uc_types::EngineError> {
        if let Some(backend) = &self.task_backend {
            let tasks = backend.list_tasks().await?;
            let count = tasks.len();
            for task in tasks {
                self.tasks.insert(task.id.0.clone(), task);
            }
            tracing::info!("Recovered {} tasks from backend", count);
            Ok(count)
        } else {
            Ok(0)
        }
    }

    /// Submit a new task: create it with a single subtask (InProgress status), store, and return.
    /// Production code uses `submit_task_pending` (Planning, no subtasks) and lets
    /// the Python Orchestrator handle decomposition.
    /// ponytail: this creates one subtask for backward compat with existing tests.
    pub fn submit_task(&mut self, description: String, project_id: String) -> uc_types::Task {
        let task_id = uc_types::TaskId::new();
        let now = chrono::Utc::now();
        let subtask_id = uc_types::TaskId::new();

        let subtask = uc_types::Subtask {
            id: subtask_id.clone(),
            parent_id: task_id.clone(),
            description: description.clone(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        };

        let task = uc_types::Task {
            id: task_id.clone(),
            description: description.clone(),
            project_id,
            status: uc_types::TaskStatus::InProgress,
            subtasks: vec![subtask],
            created_at: now,
            updated_at: now,
        };

        // Record TaskCreated event
        self.record_event_with_subject(
            uc_engine::AgentEventType::TaskCreated {
                task_id: task_id.clone(),
                description: description.clone(),
            },
            &format!("task.{}", task_id.0),
        );

        // Record subtask events
        for st in &task.subtasks {
            self.record_event_with_subject(
                uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: task_id.clone(),
                    subtask_id: st.id.clone(),
                    worker_id: uc_types::WorkerId::new(),
                },
                &format!("task.{}", task_id.0),
            );
        }

        let task_id_str = task.id.0.clone();
        self.tasks.insert(task_id_str, task.clone());
        self.persist_new_task(&task);
        task
    }

    /// Create a task in Planning status, awaiting NATS-based decomposition.
    ///
    /// Used when NATS is available — the task is created with no subtasks
    /// and status Planning. The Python Orchestrator will decompose it and
    /// send back an update via `uc.task.update`.
    pub fn submit_task_pending(
        &mut self,
        description: String,
        project_id: String,
    ) -> (uc_types::Task, Vec<TaskEvent>) {
        let task_id = uc_types::TaskId::new();
        let now = chrono::Utc::now();

        let task = uc_types::Task {
            id: task_id.clone(),
            description: description.clone(),
            project_id,
            status: uc_types::TaskStatus::Planning,
            subtasks: Vec::new(),
            created_at: now,
            updated_at: now,
        };

        // Record TaskCreated event
        let proto = self.record_event_with_subject(
            uc_engine::AgentEventType::TaskCreated {
                task_id: task_id.clone(),
                description,
            },
            &format!("task.{}", task_id.0),
        );

        let task_id_str = task.id.0.clone();
        self.tasks.insert(task_id_str, task.clone());
        self.persist_new_task(&task);
        (task, vec![proto])
    }

    /// Get a task by ID.
    pub fn get_task(&self, task_id: &str) -> Option<&uc_types::Task> {
        self.tasks.get(task_id)
    }

    /// List all tasks.
    pub fn list_tasks(&self) -> Vec<uc_types::Task> {
        self.tasks.values().cloned().collect()
    }

    /// Pause a task. Only tasks in InProgress or Planning status can be paused.
    pub fn pause_task(&mut self, task_id: &str) -> Result<uc_types::Task, String> {
        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            uc_types::TaskStatus::InProgress | uc_types::TaskStatus::Planning => {
                task.status = uc_types::TaskStatus::Paused;
                task.updated_at = chrono::Utc::now();
                let task = task.clone();
                self.persist_task(&task);
                Ok(task)
            }
            other => Err(format!(
                "Cannot pause task in {} status (expected InProgress or Planning)",
                task_status_to_proto(other)
            )),
        }
    }

    /// Resume a task. Only tasks in Paused status can be resumed.
    pub fn resume_task(&mut self, task_id: &str) -> Result<uc_types::Task, String> {
        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            uc_types::TaskStatus::Paused => {
                task.status = uc_types::TaskStatus::InProgress;
                task.updated_at = chrono::Utc::now();
                let task = task.clone();
                self.persist_task(&task);
                Ok(task)
            }
            other => Err(format!(
                "Cannot resume task in {} status (expected Paused)",
                task_status_to_proto(other)
            )),
        }
    }

    /// Cancel a task. Tasks in InProgress, Planning, or Paused status can be cancelled.
    /// Marks running/pending subtasks as Failed and sets the task to Failed.
    pub fn cancel_task(&mut self, task_id: &str) -> Result<uc_types::Task, String> {
        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            uc_types::TaskStatus::InProgress
            | uc_types::TaskStatus::Planning
            | uc_types::TaskStatus::Paused => {
                task.status = uc_types::TaskStatus::Failed;
                task.updated_at = chrono::Utc::now();
                // Mark running/pending subtasks as Failed
                for st in &mut task.subtasks {
                    if matches!(
                        st.status,
                        uc_types::SubtaskStatus::InProgress
                            | uc_types::SubtaskStatus::Pending
                            | uc_types::SubtaskStatus::Assigned
                    ) {
                        // Drop assigned-at tracking so the entry doesn't leak
                        // (we bypass update_subtask_status here, which would
                        // otherwise clear it on the Assigned→Failed transition).
                        if st.status == uc_types::SubtaskStatus::Assigned {
                            self.assigned_subtask_times.remove(&st.id.0);
                        }
                        st.status = uc_types::SubtaskStatus::Failed;
                    }
                }
                let task = task.clone();
                self.persist_task(&task);
                Ok(task)
            }
            uc_types::TaskStatus::Failed => Err("Task is already failed".to_string()),
            uc_types::TaskStatus::Completed => {
                Err("Cannot cancel task in Completed state".to_string())
            }
            uc_types::TaskStatus::Created => Err("Cannot cancel task in Created state".to_string()),
        }
    }

    /// Update an existing task's status and subtasks via gRPC UpdateTask RPC.
    ///
    /// Performs full upsert on subtasks: matches by ID, updates status/result,
    /// adds new subtasks. Records a TaskUpdated event for WatchTask stream.
    ///
    /// If the task does not exist AND `description` is non-empty, creates a new
    /// task with the given `task_id`, `description`, `project_id`, `status`, and
    /// subtasks. This enables the orchestrator to re-create tasks after a server
    /// restart using a single `updateTask` call (no `submitTask` needed).
    pub fn update_task(
        &mut self,
        task_id: &str,
        status: &str,
        subtasks: Vec<uc_types::Subtask>,
        description: &str,
        project_id: &str,
    ) -> Result<(uc_types::Task, Vec<uc_engine::AgentEventType>), String> {
        // Create-if-not-exists: when description is non-empty and task not found,
        // insert a new task with the client-provided task_id (preserving the
        // orchestrator's original ID — no new ID generation).
        let mut is_new_task = false;
        if !self.tasks.contains_key(task_id) && !description.is_empty() {
            is_new_task = true;
            let now = chrono::Utc::now();
            let task = uc_types::Task {
                id: uc_types::TaskId(task_id.to_string()),
                description: description.to_string(),
                project_id: project_id.to_string(),
                status: proto_status_to_task_status(status)
                    .unwrap_or(uc_types::TaskStatus::Created),
                subtasks: subtasks.clone(),
                created_at: now,
                updated_at: now,
            };
            self.record_event_with_subject(
                uc_engine::AgentEventType::TaskCreated {
                    task_id: task.id.clone(),
                    description: description.to_string(),
                },
                &format!("task.{}", task_id),
            );
            let task_id_str = task.id.0.clone();
            self.tasks.insert(task_id_str, task);
        }

        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;

        // Update status — fail-loud on an unrecognized status string instead
        // of silently keeping the old status while refreshing updated_at
        // (which masked the rejected update as a successful no-op timestamp
        // bump). proto_status_to_task_status returns a clear Err message.
        let parsed = proto_status_to_task_status(status)?;
        task.status = parsed;
        task.updated_at = chrono::Utc::now();

        // Collect subtask state transitions for event emission
        // ponytail: collect before mutation, emit after
        let mut subtask_transitions: Vec<(
            uc_types::TaskId,
            uc_types::SubtaskStatus,
            uc_types::SubtaskStatus,
        )> = Vec::new();
        let mut new_subtask_ids: Vec<uc_types::TaskId> = Vec::new();

        // Upsert subtasks: update existing, add new
        for st in subtasks {
            if let Some(existing) = task.subtasks.iter_mut().find(|s| s.id == st.id) {
                if existing.status != st.status {
                    subtask_transitions.push((
                        st.id.clone(),
                        existing.status.clone(),
                        st.status.clone(),
                    ));
                }
                existing.status = st.status;
                existing.result = st.result.clone();
                if !st.description.is_empty() {
                    existing.description = st.description;
                }
                if !st.depends_on.is_empty() {
                    existing.depends_on = st.depends_on;
                }
                if st.assigned_worker.is_some() {
                    existing.assigned_worker = st.assigned_worker.clone();
                }
            } else {
                new_subtask_ids.push(st.id.clone());
                task.subtasks.push(st);
            }
        }

        let updated = task.clone();

        // Build all events to record + broadcast
        let mut events: Vec<uc_engine::AgentEventType> = Vec::new();

        // TaskUpdated event
        events.push(uc_engine::AgentEventType::TaskUpdated {
            task_id: uc_types::TaskId(task_id.to_string()),
            status: status.to_string(),
        });

        // Subtask state transition events
        let tid = uc_types::TaskId(task_id.to_string());
        for (subtask_id, _old_status, new_status) in subtask_transitions {
            match new_status {
                uc_types::SubtaskStatus::InProgress => {
                    events.push(uc_engine::AgentEventType::SubtaskStarted {
                        task_id: tid.clone(),
                        subtask_id,
                        worker_id: uc_types::WorkerId::new(),
                    });
                }
                uc_types::SubtaskStatus::Completed => {
                    events.push(uc_engine::AgentEventType::SubtaskCompleted {
                        task_id: tid.clone(),
                        subtask_id,
                        summary: String::new(),
                        success: true,
                        modified_files: Vec::new(),
                        output: String::new(),
                        simulated: false,
                    });
                }
                uc_types::SubtaskStatus::Failed => {
                    events.push(uc_engine::AgentEventType::SubtaskFailed {
                        task_id: tid.clone(),
                        subtask_id,
                        error: String::new(),
                        recoverable: false,
                        stderr_tail: String::new(),
                        recent_tools: String::new(),
                    });
                }
                _ => {} // Pending, Assigned, Conflicted — no event
            }
        }

        // SubtaskAssigned for newly added subtasks
        for subtask_id in new_subtask_ids {
            events.push(uc_engine::AgentEventType::SubtaskAssigned {
                task_id: tid.clone(),
                subtask_id,
                worker_id: uc_types::WorkerId::new(),
            });
        }

        // Record all events to EventStore
        for event in &events {
            self.record_event(event.clone());
        }

        // Persist to backend: submit_task (INSERT) for newly-created tasks,
        // update_task (upsert) for existing ones. Both persist paths are
        // fire-and-forget; the first write uses the plain INSERT for clarity.
        if is_new_task {
            self.persist_new_task(&updated);
        } else {
            self.persist_task(&updated);
        }

        Ok((updated, events))
    }

    /// Read events from the given offset (from inline log).
    /// For persistent reads, use `event_store().read_from()` instead.
    pub fn read_events_from(&self, offset: usize) -> Vec<uc_engine::AgentEventType> {
        if offset >= self.events.len() {
            Vec::new()
        } else {
            self.events[offset..].to_vec()
        }
    }

    /// Get current event count (from inline log).
    pub fn event_count(&self) -> usize {
        self.events.len()
    }

    /// Access the EventStore for persistent reads.
    pub fn event_store(&self) -> &Arc<dyn uc_engine::EventStore> {
        &self.event_store
    }

    /// Apply a status update from NATS (`uc.task.update`).
    ///
    /// Updates the task's status, subtask statuses, and result.
    /// Performs full upsert on subtasks: updates description, depends_on, and
    /// result in addition to status and assigned_worker. New subtasks are
    /// created with all provided fields.
    /// If the task does not exist, the subscriber may provide complete-snapshot
    /// metadata to rehydrate it; updates without that metadata are ignored.
    pub fn apply_update(&mut self, update: &NatsTaskUpdate) {
        self.apply_update_with_metadata(update, None, None);
    }

    /// Apply a task update and optionally use complete-snapshot metadata to
    /// rehydrate an unknown task.
    ///
    /// The NATS stream can outlive the gateway's in-memory TaskStore. A full
    /// Python snapshot contains enough context to reconstruct that task with
    /// its original ID. A partial worker result intentionally cannot do this:
    /// it only describes one subtask and would create an incomplete, falsely
    /// authoritative parent task.
    fn apply_update_with_metadata(
        &mut self,
        update: &NatsTaskUpdate,
        description: Option<&str>,
        project_id: Option<&str>,
    ) {
        if !self.tasks.contains_key(&update.task_id) {
            if update.partial {
                tracing::warn!(
                    task_id = %update.task_id,
                    "Received partial NATS update for unknown task, ignoring"
                );
                return;
            }

            let description = match description {
                Some(description) if !description.is_empty() => description,
                _ => {
                    tracing::warn!(
                        task_id = %update.task_id,
                        "Received complete NATS update without task description, ignoring"
                    );
                    return;
                }
            };
            let project_id = match project_id {
                Some(project_id) => project_id,
                None => {
                    tracing::warn!(
                        task_id = %update.task_id,
                        "Received complete NATS update without project ID, ignoring"
                    );
                    return;
                }
            };

            let now = chrono::Utc::now();
            let status = match task_status_from_str(&update.status) {
                Some(status) => status,
                None => {
                    tracing::warn!(
                        task_id = %update.task_id,
                        status = %update.status,
                        "Unknown task status while rehydrating NATS task, using Created"
                    );
                    uc_types::TaskStatus::Created
                }
            };
            let task = uc_types::Task {
                id: uc_types::TaskId(update.task_id.clone()),
                description: description.to_string(),
                project_id: project_id.to_string(),
                status,
                subtasks: update
                    .subtasks
                    .iter()
                    .map(|subtask| nats_subtask_to_domain(&update.task_id, subtask))
                    .collect(),
                created_at: now,
                updated_at: now,
            };
            self.record_event_with_subject(
                uc_engine::AgentEventType::TaskCreated {
                    task_id: task.id.clone(),
                    description: task.description.clone(),
                },
                &format!("task.{}", update.task_id),
            );
            for subtask in &task.subtasks {
                if subtask.status == uc_types::SubtaskStatus::Assigned {
                    self.assigned_subtask_times
                        .insert(subtask.id.0.clone(), now);
                }
            }
            self.tasks.insert(update.task_id.clone(), task);
            tracing::info!(
                task_id = %update.task_id,
                "Rehydrated task from complete NATS snapshot"
            );
        }

        let task = match self.tasks.get_mut(&update.task_id) {
            Some(t) => t,
            None => {
                tracing::warn!(
                    task_id = %update.task_id,
                    "Received NATS update for unknown task, ignoring"
                );
                return;
            }
        };

        // Update task status
        let task_status_valid = if let Some(status) = task_status_from_str(&update.status) {
            task.status = status;
            true
        } else {
            tracing::warn!(
                task_id = %update.task_id,
                status = %update.status,
                "Unknown task status in NATS update, ignoring status field"
            );
            false
        };

        // Update result if provided
        if update.result.is_some() {
            // Task-level result is not directly stored in the current Task struct,
            // but we update the timestamp to reflect the change.
        }

        // T3 (#639): graph-plane fan-out collected during the subtask loop,
        // emitted after the `task` borrow ends (fire-and-forget, `None` sink
        // = zero overhead; legacy behavior above and below untouched).
        enum PendingGraphVerb {
            Heartbeat,
            /// Terminal success: `(result_ref, usage, steps)`. All three come
            /// from the same subtask entry and all three are needed by
            /// `on_commit` — `usage` since T15 #660, `steps` since T18 #668
            /// (the per-step disaggregation of the same report).
            Commit(
                Option<String>,
                Option<uc_types::SubtaskUsage>,
                Option<Vec<uc_types::StepUsage>>,
            ),
            Fail(&'static str),
        }
        let mut graph_fanout: Vec<(String, u32, PendingGraphVerb)> = Vec::new();
        // T4 #640: late-result fencing rejects, collected during the loop
        // (subtask_id, received_attempt, current_attempt) — settled after the
        // `task` borrow ends.
        let mut stale_rejects: Vec<(String, u64, u64)> = Vec::new();
        let graph_verb_for = |status: &uc_types::SubtaskStatus, prev: &uc_types::SubtaskStatus| {
            if status == prev {
                return None;
            }
            match status {
                uc_types::SubtaskStatus::InProgress => Some(PendingGraphVerb::Heartbeat),
                uc_types::SubtaskStatus::Completed => {
                    Some(PendingGraphVerb::Commit(None, None, None))
                }
                uc_types::SubtaskStatus::Failed => Some(PendingGraphVerb::Fail("worker_failed")),
                uc_types::SubtaskStatus::Conflicted => Some(PendingGraphVerb::Fail("conflicted")),
                _ => None,
            }
        };

        // Update subtasks — full upsert
        for subtask_update in &update.subtasks {
            if let Some(subtask) = task
                .subtasks
                .iter_mut()
                .find(|st| st.id.0 == subtask_update.subtask_id)
            {
                // T4 #640: late-result fencing. A worker-sourced (partial)
                // update stamped with an attempt OLDER than the subtask's
                // current one comes from a fenced/re-dispatched attempt — its
                // redelivery raced with the replacement dispatch. Reject the
                // whole entry: no status change, no result overwrite, no
                // graph verb. Attempt equality/greater passes (the tracker
                // may lag the wire during re-dispatch), and unstamped legacy
                // updates are never fenced (upgrade-window compat).
                if update.partial {
                    if let Some(received) = subtask_update.attempt_id {
                        let current = subtask.dispatch_retry_count as u64;
                        if received < current {
                            stale_rejects.push((subtask.id.0.clone(), received, current));
                            continue;
                        }
                    }
                }
                // Existing subtask — update all provided fields
                if let Some(status) = subtask_status_from_str(&subtask_update.status) {
                    let prev_status = subtask.status.clone();
                    let verb = graph_verb_for(&status, &prev_status);
                    let attempt = subtask.dispatch_retry_count;
                    // Clear assigned-at tracking on any transition out of Assigned
                    // (we bypass update_subtask_status here, which would otherwise
                    // clear it). This is the highest-frequency path (worker pick-up
                    // sends Assigned→InProgress), so a leak here accumulates fast.
                    if subtask.status == uc_types::SubtaskStatus::Assigned
                        && status != uc_types::SubtaskStatus::Assigned
                    {
                        self.assigned_subtask_times.remove(&subtask.id.0);
                    } else if status == uc_types::SubtaskStatus::Assigned
                        && subtask.status != uc_types::SubtaskStatus::Assigned
                    {
                        self.assigned_subtask_times
                            .insert(subtask.id.0.clone(), chrono::Utc::now());
                    }
                    subtask.status = status;
                    if let Some(verb) = verb {
                        let verb = match verb {
                            PendingGraphVerb::Commit(..) => PendingGraphVerb::Commit(
                                subtask_update
                                    .result
                                    .clone()
                                    .or_else(|| subtask.result.as_ref().map(|r| r.summary.clone())),
                                // T15 (#660): the wire's usage is authoritative
                                // for this report; otherwise keep whatever the
                                // domain row already carries (a re-published
                                // snapshot). Never synthesized.
                                subtask_update.usage.clone().or_else(|| {
                                    subtask.result.as_ref().and_then(|r| r.usage.clone())
                                }),
                                // T18 #668: the per-step records travel
                                // alongside the block they disaggregate, with
                                // the same "wire first, else what we already
                                // hold" precedence. The domain `SubtaskResult`
                                // does not carry them (nothing reads them from
                                // there — the graph verb consumes the wire
                                // directly), so `subtask.result` has nothing to
                                // fall back to and this stays a pass-through.
                                subtask_update.steps.clone(),
                            ),
                            other => other,
                        };
                        graph_fanout.push((subtask.id.0.clone(), attempt, verb));
                    }
                } else {
                    tracing::warn!(
                        subtask_id = %subtask_update.subtask_id,
                        status = %subtask_update.status,
                        "Unknown subtask status in NATS update, ignoring"
                    );
                }
                if let Some(worker) = &subtask_update.assigned_worker {
                    subtask.assigned_worker = Some(uc_types::WorkerId(worker.clone()));
                }
                if let Some(desc) = &subtask_update.description {
                    subtask.description = desc.clone();
                }
                if let Some(deps) = &subtask_update.depends_on {
                    subtask.depends_on = deps.iter().map(|d| uc_types::TaskId(d.clone())).collect();
                }
                if let Some(result_str) = &subtask_update.result {
                    // ponytail: derive success from parsed subtask status — the raw
                    // string is CamelCase ("Failed"), so a naive `!= "failed"` check
                    // would always be true and record failed subtasks as successful.
                    let success = !matches!(
                        subtask_status_from_str(&subtask_update.status),
                        Some(uc_types::SubtaskStatus::Failed)
                    );
                    subtask.result = Some(uc_types::SubtaskResult {
                        subtask_id: subtask.id.clone(),
                        worker_id: subtask.assigned_worker.clone().unwrap_or_default(),
                        modified_files: Vec::new(),
                        summary: result_str.clone(),
                        success,
                        completed_at: chrono::Utc::now(),
                        result: Some(result_str.clone()),
                        // T15 (#660): the reporter's usage is what makes the
                        // terminal event's cost/tokens non-NULL. Absent stays
                        // absent — nothing is synthesized here.
                        usage: subtask_update.usage.clone(),

                        review: None,
                    });
                }
            } else {
                // New subtask from Python Orchestrator — use the same
                // conversion as the rehydration path.
                let new_subtask = nats_subtask_to_domain(&task.id.0, subtask_update);
                if let Some(verb) =
                    graph_verb_for(&new_subtask.status, &uc_types::SubtaskStatus::Pending)
                {
                    graph_fanout.push((
                        new_subtask.id.0.clone(),
                        new_subtask.dispatch_retry_count,
                        verb,
                    ));
                }
                task.subtasks.push(new_subtask);
                // If a brand-new subtask arrives already Assigned (rare — usually
                // Pending until dispatch_ready_subtasks marks it), track assigned-at
                // so reassign_stale_assigned_subtasks can revert it if stuck.
                if task
                    .subtasks
                    .last()
                    .is_some_and(|st| st.status == uc_types::SubtaskStatus::Assigned)
                {
                    self.assigned_subtask_times
                        .insert(subtask_update.subtask_id.clone(), chrono::Utc::now());
                }
            }
        }

        // ponytail: derive terminal task status from subtask states. Python's
        // subtask-result publishes carry task-level status "InProgress" (the
        // reporter's view mid-task) and the task-level Completed/Failed
        // transition is never published on uc.task.update — so a task whose
        // subtasks ALL completed stayed InProgress in the gateway forever,
        // until mark_stale_tasks_failed killed it during a heartbeat gap
        // (worker restarts). Deriving here makes every subtask terminal
        // update settle the task; an explicit terminal status from the
        // update still wins (paused/cancelled transitions are non-terminal
        // for this purpose and never overridden while subtasks are pending).
        // Partial worker-only results must not infer completeness from the
        // currently known subset of subtasks.
        if task_status_valid
            && !update.partial
            && (task.status == uc_types::TaskStatus::InProgress
                || task.status == uc_types::TaskStatus::Planning)
        {
            let terminal = |st: &uc_types::Subtask| {
                matches!(
                    st.status,
                    uc_types::SubtaskStatus::Completed
                        | uc_types::SubtaskStatus::Failed
                        | uc_types::SubtaskStatus::Conflicted
                )
            };
            if !task.subtasks.is_empty() && task.subtasks.iter().all(terminal) {
                task.status = if task
                    .subtasks
                    .iter()
                    .all(|st| st.status == uc_types::SubtaskStatus::Completed)
                {
                    uc_types::TaskStatus::Completed
                } else {
                    uc_types::TaskStatus::Failed
                };
                tracing::info!(
                    task_id = %task.id.0,
                    status = ?task.status,
                    "Derived terminal task status from subtask states"
                );
            }
        }

        task.updated_at = chrono::Utc::now();
        let task_snapshot = task.clone();
        // Bound the in-memory task map (terminal tasks may have just appeared).
        self.evict_completed_tasks();
        // Persist the updated task to the backend (fire-and-forget upsert).
        self.persist_task(&task_snapshot);
        // T4 #640: settle the late-result fencing rejects — count them and
        // leave a user-visible event per rejection (node state untouched).
        if !stale_rejects.is_empty() {
            self.stale_dispatch_dropped += stale_rejects.len() as u64;
            for (subtask_id, received, current) in &stale_rejects {
                tracing::warn!(
                    task_id = %task_snapshot.id.0,
                    subtask_id = %subtask_id,
                    received_attempt = received,
                    current_attempt = current,
                    stale_dispatch_dropped = self.stale_dispatch_dropped,
                    "Rejected late result from a fenced/re-dispatched attempt (T4 #640)"
                );
                self.record_event(uc_engine::AgentEventType::TaskUpdated {
                    task_id: task_snapshot.id.clone(),
                    status: "stale_result_rejected".to_string(),
                });
            }
        }
        // T3: emit the collected graph-plane verbs now that the `task`
        // borrow is dead (fire-and-forget; never fails the legacy update).
        let graph_id = task_snapshot.id.0.clone();
        for (node_id, attempt, verb) in graph_fanout {
            match verb {
                PendingGraphVerb::Heartbeat => {
                    self.fanout_graph_heartbeat(&graph_id, &node_id, attempt);
                }
                PendingGraphVerb::Commit(result, usage, steps) => {
                    self.fanout_graph_commit(&graph_id, &node_id, attempt, result, usage, steps);
                }
                PendingGraphVerb::Fail(reason) => {
                    self.fanout_graph_fail(&graph_id, &node_id, attempt, reason);
                }
            }
        }
    }

    /// Record an event from NATS (`uc.task.event`).
    ///
    /// Pushes the event into the inline log AND appends to EventStore.
    pub fn record_event(&mut self, event: uc_engine::AgentEventType) {
        // Derive subject from event type
        let subject = match &event {
            uc_engine::AgentEventType::TaskCreated { task_id, .. } => format!("task.{}", task_id.0),
            uc_engine::AgentEventType::SubtaskAssigned { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::SubtaskStarted { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::SubtaskProgress { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::SubtaskCompleted { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::SubtaskFailed { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::TaskPaused { task_id } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::TaskResumed { task_id } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::TaskCancelled { task_id } => {
                format!("task.{}", task_id.0)
            }
            uc_engine::AgentEventType::TaskUpdated { task_id, .. } => {
                format!("task.{}", task_id.0)
            }
            _ => "events".to_string(),
        };
        self.events.push(event.clone());
        // Cap the inline log to prevent unbounded growth (OOM on long runs).
        if self.events.len() > Self::INLINE_EVENTS_MAX {
            let drop_n = self.events.len() - Self::INLINE_EVENTS_MAX;
            self.events.drain(0..drop_n);
        }
        // ponytail: spawn is fire-and-forget; if no runtime, skip (tests)
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            let es = self.event_store.clone();
            handle.spawn(async move {
                let _ = es.append(&subject, &event).await;
            });
        }
    }

    /// Update the last heartbeat timestamp from the Python NATS consumer.
    pub fn update_last_heartbeat(&mut self) {
        self.last_heartbeat = Some(chrono::Utc::now());
    }

    /// Update per-worker heartbeat timestamp.
    ///
    /// `stale_dispatch_dropped` (T4 #640 / D7) is the worker's cumulative
    /// count of old-envelope dispatches it term-dropped; `None` = legacy
    /// heartbeat without the field. Monotonic max is kept per worker.
    pub fn update_worker_heartbeat(
        &mut self,
        worker_id: &str,
        stale_dispatch_dropped: Option<u64>,
    ) {
        self.worker_heartbeats
            .insert(worker_id.to_string(), chrono::Utc::now());
        if let Some(n) = stale_dispatch_dropped {
            let slot = self
                .worker_stale_dispatch_dropped
                .entry(worker_id.to_string())
                .or_insert(0);
            if n > *slot {
                *slot = n;
            }
        }
    }

    /// Gateway-side count of late results rejected by attempt fencing (T4).
    pub fn stale_dispatch_dropped(&self) -> u64 {
        self.stale_dispatch_dropped
    }

    /// Per-worker cumulative stale-dispatch drops reported via heartbeat.
    pub fn worker_stale_dispatch_dropped(&self) -> &HashMap<String, u64> {
        &self.worker_stale_dispatch_dropped
    }

    /// Access per-worker heartbeat timestamps.
    pub fn worker_heartbeats(&self) -> &HashMap<String, chrono::DateTime<chrono::Utc>> {
        &self.worker_heartbeats
    }

    /// Find workers whose heartbeats are older than `timeout`.
    /// Returns their IDs.
    pub fn mark_stale_workers(&mut self, timeout: std::time::Duration) -> Vec<String> {
        let now = chrono::Utc::now();
        let stale: Vec<String> = self
            .worker_heartbeats
            .iter()
            .filter(|(_, ts)| (now - *ts).to_std().unwrap_or_default() > timeout)
            .map(|(id, _)| id.clone())
            .collect();
        // Remove stale workers from heartbeat map
        for id in &stale {
            self.worker_heartbeats.remove(id);
        }
        stale
    }

    /// Bridge one graph-plane swept attempt back into the legacy store
    /// (T6 #642). The sweep already fenced the attempt on the graph rows
    /// (fail + epoch bump), so this only mirrors the outcome onto the
    /// legacy subtask — the dispatch read path stays legacy until the
    /// planes fully converge.
    ///
    /// `rearmed` (retry budget left) reverts the subtask to `Pending`;
    /// the budget-exhausted outcome marks it `Failed`. A subtask that is
    /// no longer `InProgress`/`Assigned` (e.g. a commit landed between the
    /// sweep and this bridge) is never touched — committed winners are
    /// immutable. Returns `true` when the legacy row moved (the caller
    /// re-dispatches the task).
    pub fn revert_swept_subtask(&mut self, task_id: &str, subtask_id: &str, rearmed: bool) -> bool {
        let new_status = if rearmed {
            uc_types::SubtaskStatus::Pending
        } else {
            uc_types::SubtaskStatus::Failed
        };
        let Some(task) = self.tasks.get_mut(task_id) else {
            return false;
        };
        let Some(st) = task.subtasks.iter_mut().find(|s| s.id.0 == subtask_id) else {
            return false;
        };
        if !matches!(
            st.status,
            uc_types::SubtaskStatus::InProgress | uc_types::SubtaskStatus::Assigned
        ) {
            return false;
        }
        // The graph fail already rode the sweep itself; this transition
        // only leaves the Assigned-at tracker consistent.
        self.assigned_subtask_times.remove(subtask_id);
        st.status = new_status;
        st.assigned_worker = None;
        task.updated_at = chrono::Utc::now();
        let snapshot = task.clone();
        let reason = if rearmed {
            "heartbeat_timeout"
        } else {
            "attempts_exhausted"
        };
        tracing::warn!(
            task_id = %task_id,
            subtask_id = %subtask_id,
            rearmed,
            "Graph-plane sweep bridged to legacy store ({reason})"
        );
        self.persist_task(&snapshot);
        true
    }

    /// Get the last heartbeat timestamp.
    pub fn last_heartbeat(&self) -> Option<chrono::DateTime<chrono::Utc>> {
        self.last_heartbeat
    }

    /// T7 #643 — legacy mirror of a node-level cancel: the listed subtasks
    /// (the graph plane's CANCELLED closure) go to `Failed` — the wire
    /// vocabulary has no distinct cancelled row (T4 mapping: mirror
    /// `cancelled` rides Failed semantics), so the graph `CANCELLED` state
    /// and a legacy `Failed` row are the same fact on the wire. Completed
    /// subtasks are untouched. Returns how many rows moved.
    pub fn fail_subtasks(&mut self, task_id: &str, subtask_ids: &[String]) -> usize {
        let Some(task) = self.tasks.get_mut(task_id) else {
            return 0;
        };
        let mut moved = 0;
        for st in &mut task.subtasks {
            if !subtask_ids.iter().any(|id| id == &st.id.0) {
                continue;
            }
            if matches!(
                st.status,
                uc_types::SubtaskStatus::InProgress
                    | uc_types::SubtaskStatus::Pending
                    | uc_types::SubtaskStatus::Assigned
            ) {
                if st.status == uc_types::SubtaskStatus::Assigned {
                    self.assigned_subtask_times.remove(&st.id.0);
                }
                st.status = uc_types::SubtaskStatus::Failed;
                st.assigned_worker = None;
                moved += 1;
            }
        }
        if moved > 0 {
            task.updated_at = chrono::Utc::now();
            let snapshot = task.clone();
            self.persist_task(&snapshot);
        }
        moved
    }

    /// Get subtasks that are ready to be dispatched for a given task.
    ///
    /// A subtask is "ready" when:
    /// - Its status is `Pending`
    /// - All subtasks it depends on have status `Completed`
    pub fn get_ready_subtasks(&self, task_id: &str) -> Vec<uc_types::Subtask> {
        let task = match self.tasks.get(task_id) {
            Some(t) => t,
            None => return Vec::new(),
        };

        // Only dispatch if task is actively running
        if task.status != uc_types::TaskStatus::InProgress {
            return Vec::new();
        }

        let completed_ids: std::collections::HashSet<&str> = task
            .subtasks
            .iter()
            .filter(|st| st.status == uc_types::SubtaskStatus::Completed)
            .map(|st| st.id.0.as_str())
            .collect();

        task.subtasks
            .iter()
            .filter(|st| st.status == uc_types::SubtaskStatus::Pending)
            .filter(|st| {
                st.depends_on
                    .iter()
                    .all(|dep| completed_ids.contains(dep.0.as_str()))
            })
            .cloned()
            .collect()
    }

    /// Update a subtask's status within a task. No-op if task/subtask not found.
    pub fn update_subtask_status(
        &mut self,
        task_id: &str,
        subtask_id: &str,
        new_status: uc_types::SubtaskStatus,
    ) {
        let task = match self.tasks.get_mut(task_id) {
            Some(t) => t,
            None => return,
        };
        let mut schedule_attempt: Option<u32> = None;
        {
            let st = match task.subtasks.iter_mut().find(|s| s.id.0 == subtask_id) {
                Some(s) => s,
                None => return,
            };
            // T3 graph-plane waypoint: both dispatch mouths
            // (`publish_ready_subtasks` / `dispatch_ready_subtasks`) mark
            // Assigned exactly here, so the schedule verb rides the single
            // shared mutation (first-time marking only — a re- Assigned of
            // an already-Assigned subtask is not a new attempt).
            let going_assigned = new_status == uc_types::SubtaskStatus::Assigned
                && st.status != uc_types::SubtaskStatus::Assigned;
            if going_assigned {
                schedule_attempt = Some(st.dispatch_retry_count);
            }
            // Track Assigned-at so stale Assigned subtasks (no worker ever
            // picked them up) can be reverted to Pending by the heartbeat
            // monitor. Clear on any transition out of Assigned.
            if new_status == uc_types::SubtaskStatus::Assigned {
                self.assigned_subtask_times
                    .insert(subtask_id.to_string(), chrono::Utc::now());
            } else if st.status == uc_types::SubtaskStatus::Assigned {
                self.assigned_subtask_times.remove(subtask_id);
            }
            st.status = new_status;
            task.updated_at = chrono::Utc::now();
        }
        // st borrow dropped — safe to clone task for persist.
        let task_snapshot = task.clone();
        if let Some(attempt) = schedule_attempt {
            self.fanout_graph_schedule(task_id, subtask_id, attempt, None);
        }
        self.persist_task(&task_snapshot);
    }

    /// Increment a subtask's dispatch_retry_count within a task.
    /// Returns the new retry count, or None if task/subtask not found.
    pub fn increment_dispatch_retry(&mut self, task_id: &str, subtask_id: &str) -> Option<u32> {
        let task = self.tasks.get_mut(task_id)?;
        let retry_count = {
            let st = task.subtasks.iter_mut().find(|s| s.id.0 == subtask_id)?;
            st.dispatch_retry_count += 1;
            task.updated_at = chrono::Utc::now();
            st.dispatch_retry_count
        };
        let task_snapshot = task.clone();
        self.persist_task(&task_snapshot);
        Some(retry_count)
    }

    /// Update the status of a task by ID.
    ///
    /// Used by tests and by worker-death handlers to change task status.
    /// Returns the previous status, or None if the task was not found.
    pub fn set_task_status(
        &mut self,
        task_id: &str,
        new_status: uc_types::TaskStatus,
    ) -> Option<uc_types::TaskStatus> {
        let task = self.tasks.get_mut(task_id)?;
        let old = task.status.clone();
        task.status = new_status;
        task.updated_at = chrono::Utc::now();
        let task_snapshot = task.clone();
        self.persist_task(&task_snapshot);
        Some(old)
    }

    /// Mark tasks as Failed if no heartbeat has been received within
    /// the specified timeout AND there are tasks in InProgress or Planning status.
    ///
    /// Returns the IDs of tasks that were marked as Failed.
    pub fn mark_stale_tasks_failed(&mut self, timeout: std::time::Duration) -> Vec<String> {
        // If we've never received a heartbeat, there's no consumer to go stale.
        let last_hb = match self.last_heartbeat {
            Some(ts) => ts,
            None => return Vec::new(),
        };

        let now = chrono::Utc::now();
        let elapsed = now.signed_duration_since(last_hb);
        if elapsed.num_milliseconds() < timeout.as_millis() as i64 {
            return Vec::new();
        }

        // Consumer is stale — mark all InProgress/Planning tasks as Failed
        let mut failed_ids = Vec::new();
        let mut snapshots: Vec<uc_types::Task> = Vec::new();
        for (id, task) in &mut self.tasks {
            match task.status {
                uc_types::TaskStatus::InProgress | uc_types::TaskStatus::Planning => {
                    task.status = uc_types::TaskStatus::Failed;
                    task.updated_at = now;
                    failed_ids.push(id.clone());
                    snapshots.push(task.clone());
                }
                _ => {}
            }
        }

        for task in &snapshots {
            self.persist_task(task);
        }

        if !failed_ids.is_empty() {
            tracing::warn!(
                elapsed_secs = elapsed.num_seconds(),
                tasks_failed = failed_ids.len(),
                "Marked tasks as Failed due to consumer heartbeat timeout"
            );
        }

        failed_ids
    }
}

// ── gRPC Server ─────────────────────────────────────────────

/// Create a `tonic_health` reporter and health service pre-configured to
/// report the `EngineService` as `Serving`.
///
/// Returns a `(HealthReporter, HealthServer)` pair. The reporter can be
/// used to update service status at runtime; the server should be
/// registered with the tonic router via `add_service`.
///
/// The `EngineService` is registered by its gRPC service name
/// so that standard health-checking clients can query it.
pub async fn health_reporter<E>() -> (
    tonic_health::server::HealthReporter,
    tonic_health::pb::health_server::HealthServer<impl tonic_health::pb::health_server::Health>,
)
where
    E: EngineApi + Send + Sync + 'static,
{
    let (mut reporter, service) = tonic_health::server::health_reporter();
    reporter
        .set_serving::<EngineServiceServer<GrpcServer<E>>>()
        .await;
    (reporter, service)
}

/// Internal shared state for the gRPC server.
struct GrpcServerInner<E: EngineApi + Send + Sync + 'static> {
    engine: E,
    task_store: Arc<Mutex<TaskStore>>,
    /// Checkpoint manager sharing TaskStore's EventStore — the activated
    /// event-sourcing recovery path.
    checkpoint_manager: Arc<uc_engine::CheckpointManager>,
    /// Worker registry — source of truth for WorkerService and capability-aware dispatch.
    worker_registry: Arc<RwLock<WorkerRegistry>>,
    /// Per-task pause-grace timers (D6, T6 #642): AbortHandle per paused
    /// task. Resume aborts the timer; firing fails the paused task's still-
    /// RUNNING graph attempts (`pause_grace_expired`) through the graph plane
    /// and bridges the outcomes back into the legacy store.
    pause_grace_timers: Arc<std::sync::Mutex<HashMap<String, tokio::task::AbortHandle>>>,
    /// NATS client for task submission and status subscriptions.
    /// Present when the `messaging` feature is enabled and NATS connection succeeded.
    #[cfg(feature = "messaging")]
    nats_client: Option<async_nats::Client>,
    /// Broadcast channel for real-time task event streaming.
    /// All event sources (NATS, local decomposition) publish here.
    /// WatchTask streams subscribe via Receiver for instant delivery.
    event_tx: broadcast::Sender<TaskEvent>,
}

/// gRPC server that delegates EngineService operations to an inner EngineApi
/// and provides TaskService via an in-memory task store.
///
/// Internally wraps state in `Arc` so both services can share it.
///
/// When the `messaging` feature is enabled and NATS is available:
/// - `submit_task()` publishes to `uc.task.submit` and creates a Planning task
/// - A background subscriber listens on `uc.task.update` and `uc.task.event`
/// - Heartbeat monitoring marks stale tasks as Failed
///
/// When NATS is unavailable, falls back to local task decomposition.
pub struct GrpcServer<E: EngineApi + Send + Sync + 'static> {
    inner: Arc<GrpcServerInner<E>>,
}

/// A business gRPC service wrapped with [`crate::AuthInterceptor`].
///
/// Each of the four business services (engine/task/dashboard/worker) is
/// wrapped in this form when `UC_DASHBOARD_TOKEN` is set. The tonic_health
/// standard service is intentionally NOT wrapped.
pub(crate) type Intercepted<S> =
    tonic::service::interceptor::InterceptedService<S, crate::AuthInterceptor>;

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Create a new gRPC server wrapping the given engine, without NATS.
    ///
    /// Falls back to local (newline-split) decomposition if NATS is
    /// unavailable.
    pub fn new(engine: E) -> Self {
        let (event_tx, _) = broadcast::channel(256);

        let event_store: Arc<dyn uc_engine::EventStore> =
            Arc::new(uc_engine::InMemoryEventStore::new());
        let task_store = Arc::new(Mutex::new(TaskStore::with_event_store(event_store.clone())));
        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));
        let checkpoint_manager = Self::build_checkpoint_manager(event_store);

        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store,
                checkpoint_manager,
                worker_registry,
                pause_grace_timers: Arc::new(std::sync::Mutex::new(HashMap::new())),
                #[cfg(feature = "messaging")]
                nats_client: None,
                event_tx,
            }),
        }
    }

    /// Build a CheckpointManager sharing an EventStore.
    /// `subject_prefix = "task."` matches TaskStore's event-recording subjects
    /// so recover() reads the same stream production events land in.
    fn build_checkpoint_manager(
        event_store: Arc<dyn uc_engine::EventStore>,
    ) -> Arc<uc_engine::CheckpointManager> {
        let config = uc_engine::CheckpointConfig {
            subject_prefix: "task.".to_string(),
            ..Default::default()
        };
        Arc::new(uc_engine::CheckpointManager::new(event_store, config))
    }

    /// Create a new gRPC server with custom task and event backends.
    ///
    /// Use this to configure PostgreSQL persistence for tasks and events.
    pub fn with_backends(
        engine: E,
        task_backend: Arc<dyn uc_engine::TaskStoreBackend>,
        event_store: Arc<dyn uc_engine::EventStore>,
    ) -> Self {
        let (event_tx, _) = broadcast::channel(256);
        let task_store = Arc::new(Mutex::new(TaskStore::with_backend(
            task_backend,
            event_store.clone(),
        )));
        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));
        let checkpoint_manager = Self::build_checkpoint_manager(event_store);

        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store,
                checkpoint_manager,
                worker_registry,
                pause_grace_timers: Arc::new(std::sync::Mutex::new(HashMap::new())),
                #[cfg(feature = "messaging")]
                nats_client: None,
                event_tx,
            }),
        }
    }

    /// Create a new gRPC server with NATS integration.
    ///
    /// Attempts to connect to NATS at the given URL. If the connection
    /// fails, logs a warning and proceeds without NATS (graceful degradation).
    ///
    /// When NATS is connected:
    /// - `submit_task()` publishes to `uc.task.submit` instead of local decomposition
    /// - A background subscriber updates TaskStore from `uc.task.update` and `uc.task.event`
    /// - A heartbeat monitor marks stale tasks as Failed
    #[cfg(feature = "messaging")]
    pub async fn with_nats(engine: E, nats_url: &str) -> Self {
        Self::with_nats_and_timeout(engine, nats_url, std::time::Duration::from_secs(120)).await
    }

    /// Create a new gRPC server with NATS integration and custom backends.
    #[cfg(feature = "messaging")]
    pub async fn with_nats_and_backends(
        engine: E,
        nats_url: &str,
        task_backend: Arc<dyn uc_engine::TaskStoreBackend>,
        event_store: Arc<dyn uc_engine::EventStore>,
    ) -> Self {
        Self::with_nats_timeout_and_backends(
            engine,
            nats_url,
            std::time::Duration::from_secs(120),
            task_backend,
            event_store,
        )
        .await
    }

    /// Create a new gRPC server with NATS integration, custom timeout, and backends.
    #[cfg(feature = "messaging")]
    pub async fn with_nats_timeout_and_backends(
        engine: E,
        nats_url: &str,
        heartbeat_timeout: std::time::Duration,
        task_backend: Arc<dyn uc_engine::TaskStoreBackend>,
        event_store: Arc<dyn uc_engine::EventStore>,
    ) -> Self {
        let nats_client = connect_nats_with_retry(nats_url).await;

        let task_store = Arc::new(Mutex::new(TaskStore::with_backend(
            task_backend,
            event_store.clone(),
        )));

        let (event_tx, _) = broadcast::channel(256);

        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));
        let checkpoint_manager = Self::build_checkpoint_manager(event_store);
        let inner = Arc::new(GrpcServerInner {
            engine,
            task_store: task_store.clone(),
            checkpoint_manager,
            worker_registry: worker_registry.clone(),
            pause_grace_timers: Arc::new(std::sync::Mutex::new(HashMap::new())),
            nats_client: nats_client.clone(),
            event_tx: event_tx.clone(),
        });

        // Spawn background subscriber and heartbeat monitor if NATS is connected
        if let Some(client) = nats_client {
            spawn_nats_subscriber(
                client.clone(),
                task_store.clone(),
                worker_registry.clone(),
                event_tx,
            );
            spawn_file_changed_subscriber(client.clone(), inner.clone());
            spawn_heartbeat_monitor(
                client,
                task_store.clone(),
                worker_registry.clone(),
                heartbeat_timeout,
            );
        }

        Self { inner }
    }

    /// Create a new gRPC server with NATS integration and custom heartbeat timeout.
    #[cfg(feature = "messaging")]
    pub async fn with_nats_and_timeout(
        engine: E,
        nats_url: &str,
        heartbeat_timeout: std::time::Duration,
    ) -> Self {
        let nats_client = connect_nats_with_retry(nats_url).await;

        let event_store: Arc<dyn uc_engine::EventStore> =
            Arc::new(uc_engine::InMemoryEventStore::new());
        let task_store = Arc::new(Mutex::new(TaskStore::with_event_store(event_store.clone())));

        let (event_tx, _) = broadcast::channel(256);

        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));
        let checkpoint_manager = Self::build_checkpoint_manager(event_store);
        let inner = Arc::new(GrpcServerInner {
            engine,
            task_store: task_store.clone(),
            checkpoint_manager,
            worker_registry: worker_registry.clone(),
            pause_grace_timers: Arc::new(std::sync::Mutex::new(HashMap::new())),
            nats_client: nats_client.clone(),
            event_tx: event_tx.clone(),
        });

        // Spawn background subscriber and heartbeat monitor if NATS is connected
        if let Some(client) = nats_client {
            spawn_nats_subscriber(
                client.clone(),
                task_store.clone(),
                worker_registry.clone(),
                event_tx,
            );
            spawn_file_changed_subscriber(client.clone(), inner.clone());
            spawn_heartbeat_monitor(
                client,
                task_store.clone(),
                worker_registry.clone(),
                heartbeat_timeout,
            );
        }

        Self { inner }
    }

    /// Convert into tonic services ready to be served.
    pub fn into_services(
        self,
    ) -> (
        EngineServiceServer<Self>,
        TaskServiceServer<Self>,
        DashboardServiceServer<Self>,
        WorkerServiceServer<Self>,
    ) {
        let engine_service = EngineServiceServer::new(Self {
            inner: self.inner.clone(),
        });
        let task_service = TaskServiceServer::new(self.clone());
        let dashboard_service = DashboardServiceServer::new(self.clone());
        let worker_service = WorkerServiceServer::new(self);
        (
            engine_service,
            task_service,
            dashboard_service,
            worker_service,
        )
    }

    /// Convert into tonic services wrapped with an auth interceptor.
    ///
    /// Each business service (engine/task/dashboard/worker) is wrapped via the
    /// generated `with_interceptor`, which validates `Authorization: Bearer
    /// <token>` on every request. The interceptor is cloned per service
    /// (clone-cheap: inner is `Arc<str>`).
    ///
    /// The tonic_health standard health service is NOT produced here and must
    /// NOT be wrapped — health probes must remain unauthenticated for kube /
    /// docker compatibility.
    #[allow(clippy::type_complexity)] // 4-tuple of distinct intercepted services is inherently complex
    pub fn into_intercepted_services(
        self,
        interceptor: crate::AuthInterceptor,
    ) -> (
        Intercepted<EngineServiceServer<Self>>,
        Intercepted<TaskServiceServer<Self>>,
        Intercepted<DashboardServiceServer<Self>>,
        Intercepted<WorkerServiceServer<Self>>,
    ) {
        let engine_service = EngineServiceServer::with_interceptor(
            Self {
                inner: self.inner.clone(),
            },
            interceptor.clone(),
        );
        let task_service = TaskServiceServer::with_interceptor(self.clone(), interceptor.clone());
        let dashboard_service =
            DashboardServiceServer::with_interceptor(self.clone(), interceptor.clone());
        let worker_service = WorkerServiceServer::with_interceptor(self, interceptor);
        (
            engine_service,
            task_service,
            dashboard_service,
            worker_service,
        )
    }

    /// Expose the NATS client for DashboardService passthrough.
    #[cfg(feature = "messaging")]
    pub fn nats_client(&self) -> Option<async_nats::Client> {
        self.inner.nats_client.clone()
    }

    #[cfg(not(feature = "messaging"))]
    pub fn nats_client(&self) -> Option<()> {
        None
    }

    /// Access the shared TaskStore.
    pub fn task_store(&self) -> &Arc<Mutex<TaskStore>> {
        &self.inner.task_store
    }

    /// Access the CheckpointManager (event-sourcing snapshot/recover).
    pub fn checkpoint_manager(&self) -> &Arc<uc_engine::CheckpointManager> {
        &self.inner.checkpoint_manager
    }

    /// Load all tasks from the backend into TaskStore's in-memory HashMap.
    /// Call once at startup (after construction, before serving) to recover
    /// tasks persisted by prior runs. No-op if no backend is configured.
    pub async fn load_tasks_from_backend(&self) -> Result<usize, uc_types::EngineError> {
        let mut store = self.inner.task_store.lock().await;
        store.load_from_backend().await
    }

    /// Access the shared WorkerRegistry.
    pub fn worker_registry(&self) -> &Arc<RwLock<WorkerRegistry>> {
        &self.inner.worker_registry
    }

    /// Access the broadcast sender for TaskEvent stream.
    pub fn event_sender(&self) -> &broadcast::Sender<TaskEvent> {
        &self.inner.event_tx
    }

    /// Access the Engine.
    pub fn engine(&self) -> &E {
        &self.inner.engine
    }

    /// Access a clone of the Engine (for spawning async tasks that need owned Engine).
    pub fn engine_clone(&self) -> E
    where
        E: Clone,
    {
        self.inner.engine.clone()
    }

    /// Publish a task status change event (pause/resume) to NATS
    /// so the Python Orchestrator can react.
    ///
    /// Also registers the message_id in the TaskStore's dedup map so
    /// the NATS subscriber skips the echo of our own message.
    #[cfg(feature = "messaging")]
    async fn publish_task_status_event(&self, task_id: &str, event_type: &str) {
        publish_task_control_event(
            self.inner.nats_client.as_ref(),
            &self.inner.task_store,
            task_id,
            event_type,
            None,
            serde_json::Map::new(),
        )
        .await;
    }

    #[cfg(not(feature = "messaging"))]
    async fn publish_task_status_event(&self, _task_id: &str, _event_type: &str) {}

    /// T7 #643 — granular variant of `publish_task_status_event`: the
    /// subtask/node identity rides the `subtask_id` slot and extra detail
    /// (attempt_id, reason, rearm state, ...) rides the `data` map. Same
    /// dedup discipline as the coarse event: the message_id is registered
    /// so the gateway's own NATS subscriber skips the echo.
    #[cfg(feature = "messaging")]
    async fn publish_task_control_event(
        &self,
        task_id: &str,
        event_type: &str,
        subtask_id: Option<&str>,
        data: serde_json::Map<String, serde_json::Value>,
    ) {
        publish_task_control_event(
            self.inner.nats_client.as_ref(),
            &self.inner.task_store,
            task_id,
            event_type,
            subtask_id,
            data,
        )
        .await;
    }

    #[cfg(not(feature = "messaging"))]
    async fn publish_task_control_event(
        &self,
        _task_id: &str,
        _event_type: &str,
        _subtask_id: Option<&str>,
        _data: serde_json::Map<String, serde_json::Value>,
    ) {
    }

    /// T7 #643 — attempt-level cancel (`subtask_id` + `attempt_no`):
    /// cancel-attempt-keep-node (D6 #635). The graph plane fences the
    /// node's RUNNING attempt (epoch bump; the standard retry budget drives
    /// RearmedToReady vs NodeFailed), the legacy mirror requeues the
    /// subtask row (rearmed → Pending) or marks it Failed, and an
    /// `attempt_cancelled` control event tells the worker to kill the
    /// running process. `attempt_no` is informational — the graph plane
    /// fences whatever attempt is currently RUNNING (the wire has no
    /// per-attempt selector; the epoch is the fence). When the node
    /// re-arms and the task is still InProgress, ready work is re-dispatched
    /// immediately — the node stays alive, only the attempt was cancelled.
    async fn cancel_task_attempt_granular(
        &self,
        task_id: &str,
        node_id: &str,
    ) -> CancelTaskResponse {
        let swept = {
            let store = self.inner.task_store.lock().await;
            match store.graph_shadow() {
                Some(sink) => sink.cancel_running_attempt(task_id, node_id).await,
                None => {
                    tracing::warn!(
                        task_id = %task_id,
                        node_id = %node_id,
                        "Attempt-level cancel: no graph shadow wired"
                    );
                    None
                }
            }
        };
        let Some(s) = swept else {
            return CancelTaskResponse {
                success: false,
                task_id: task_id.to_string(),
                status: String::new(),
                error: Some(format!(
                    "no running attempt to cancel for node {node_id} (shadow off, terminal already, or the cancel lost a race with a commit)"
                )),
            };
        };
        // Legacy mirror: requeue the subtask row when the node re-armed so
        // the dispatch read path sees a fresh Pending (committed winners
        // are never touched by the bridge).
        let bridged = {
            let mut store = self.inner.task_store.lock().await;
            store.revert_swept_subtask(task_id, node_id, s.rearmed)
        };
        // Worker kill trigger + WatchTask broadcast.
        let mut data = serde_json::Map::new();
        data.insert(
            "attempt_id".to_string(),
            serde_json::Value::String(s.attempt_id.clone()),
        );
        data.insert("rearmed".to_string(), serde_json::Value::Bool(s.rearmed));
        data.insert(
            "reason".to_string(),
            serde_json::Value::String("cancelled".to_string()),
        );
        data.insert("bridged".to_string(), serde_json::Value::Bool(bridged));
        self.publish_task_control_event(task_id, "attempt_cancelled", Some(node_id), data.clone())
            .await;
        let _ = self.inner.event_tx.send(TaskEvent {
            timestamp: chrono::Utc::now().to_rfc3339(),
            r#type: "attempt_cancelled".to_string(),
            task_id: task_id.to_string(),
            subtask_id: Some(node_id.to_string()),
            data: data
                .iter()
                .map(|(k, v)| (k.clone(), v.to_string()))
                .collect(),
        });
        if s.rearmed {
            #[cfg(feature = "messaging")]
            if let Some(client) = self.inner.nats_client.clone() {
                dispatch_ready_subtasks(
                    &self.inner.task_store,
                    &self.inner.worker_registry,
                    &client,
                    task_id,
                )
                .await;
            }
        }
        CancelTaskResponse {
            success: true,
            task_id: task_id.to_string(),
            status: if s.rearmed { "rearmed" } else { "failed" }.to_string(),
            error: None,
        }
    }

    /// T7 #643 — node-level cancel (bare `subtask_id`): the downstream
    /// dependency closure of the node (computed on the graph plane —
    /// cascade cancel is a runtime computation, not client bookkeeping)
    /// goes `CANCELLED` — terminal, no re-arm — and each cancelled node's
    /// RUNNING attempt is failed. The legacy rows mirror to `Failed`
    /// (the wire vocabulary has no distinct cancelled row — T4 mapping).
    /// Terminal nodes and completed siblings are untouched.
    async fn cancel_task_nodes_granular(
        &self,
        task_id: &str,
        roots: &[String],
    ) -> CancelTaskResponse {
        let (closure, cancelled) = {
            let store = self.inner.task_store.lock().await;
            match store.graph_shadow() {
                Some(sink) => {
                    let closure = sink.downstream_closure(task_id, roots).await;
                    let cancelled = sink.cancel_nodes(task_id, &closure).await;
                    (closure, cancelled)
                }
                None => {
                    tracing::warn!(
                        task_id = %task_id,
                        roots = ?roots,
                        "Node-level cancel: no graph shadow wired"
                    );
                    (Vec::new(), Vec::new())
                }
            }
        };
        if cancelled.is_empty() {
            return CancelTaskResponse {
                success: false,
                task_id: task_id.to_string(),
                status: String::new(),
                error: Some(
                    "nothing cancelled (no graph shadow, or every node in the closure is already terminal)"
                        .to_string(),
                ),
            };
        }
        // Legacy mirror: the cancelled subtask rows go Failed so the legacy
        // dispatch/claim read paths see the same fact (T4 mapping).
        let moved = {
            let mut store = self.inner.task_store.lock().await;
            store.fail_subtasks(task_id, &cancelled)
        };
        let cancelled_csv = cancelled.join(",");
        for root in roots {
            let mut data = serde_json::Map::new();
            data.insert(
                "cancelled_nodes".to_string(),
                serde_json::Value::String(cancelled_csv.clone()),
            );
            data.insert(
                "reason".to_string(),
                serde_json::Value::String("cancelled".to_string()),
            );
            self.publish_task_control_event(task_id, "subtask_cancelled", Some(root), data.clone())
                .await;
            let _ = self.inner.event_tx.send(TaskEvent {
                timestamp: chrono::Utc::now().to_rfc3339(),
                r#type: "subtask_cancelled".to_string(),
                task_id: task_id.to_string(),
                subtask_id: Some(root.clone()),
                data: data
                    .iter()
                    .map(|(k, v)| (k.clone(), v.to_string()))
                    .collect(),
            });
        }
        tracing::warn!(
            task_id = %task_id,
            roots = ?roots,
            closure = closure.len(),
            cancelled = cancelled.len(),
            subtasks_bridged = moved,
            "Node-level cancel applied (graph plane + legacy mirror)"
        );
        CancelTaskResponse {
            success: true,
            task_id: task_id.to_string(),
            status: "cancelled".to_string(),
            error: None,
        }
    }

    /// Dispatch ready subtasks for a task to the NATS `uc.subtask.execute` subject.
    ///
    /// Called after a task is decomposed (subtasks populated) and after each
    /// `uc.task.update` that may have completed dependencies.
    ///
    /// Marks dispatched subtasks as `Assigned` in TaskStore so they are not
    /// re-dispatched on the next call.
    #[cfg(feature = "messaging")]
    pub async fn publish_ready_subtasks(&self, task_id: &str) {
        let (ready, project_id) = {
            let mut store = self.inner.task_store.lock().await;
            let subtasks = store.get_ready_subtasks(task_id);
            let project_id = store
                .get_task(task_id)
                .map(|t| t.project_id.clone())
                .unwrap_or_default();

            // Check WorkerRegistry for capability-aware dispatch:
            // Only mark as Assigned if a matching worker exists (or no capabilities required).
            let registry = self.inner.worker_registry.read().await;
            // T12 #654 / D12 #649: locality input — the hosts already running
            // this task's other nodes. Computed once per mouth invocation
            // (inside the existing lock block: `TaskStore` is a tokio Mutex
            // and must not be re-locked here).
            let sibling_hosts = sibling_worker_hosts(&store, &registry, task_id);
            let mut dispatchable = Vec::new();
            for st in &subtasks {
                // Cap dispatch retries (mirrors dispatch_ready_subtasks): if a
                // subtask has been reverted to Pending this many times with no
                // worker picking it up, mark Failed to stop an infinite
                // dispatch storm.
                if st.dispatch_retry_count >= 3 {
                    tracing::error!(
                        subtask_id = %st.id.0,
                        retry_count = st.dispatch_retry_count,
                        "Subtask exceeded max dispatch retries (no worker picked it up), marking Failed"
                    );
                    store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Failed);
                    continue;
                }
                // Capability + scope + contract_version hard gate (T1 #637;
                // scope hard filter T8 #650): only mark Assigned / publish
                // when at least one capability-matching available worker
                // serves the task's project scope and declared the gateway's
                // contract version.
                match registry.dispatch_gate(&st.required_capabilities, &project_id) {
                    crate::worker_service::WorkerDispatchGate::Dispatch => {}
                    crate::worker_service::WorkerDispatchGate::NoCapableWorker => {
                        tracing::info!(
                            subtask_id = %st.id.0,
                            required_capabilities = ?st.required_capabilities,
                            "No worker with matching capabilities, keeping subtask Pending"
                        );
                        continue; // skip — don't mark as Assigned
                    }
                    crate::worker_service::WorkerDispatchGate::NoScopeMatchedWorker { workers } => {
                        tracing::info!(
                            subtask_id = %st.id.0,
                            project_id = %project_id,
                            scope_capable_workers = ?workers,
                            "No scope-matching worker serves this project_id, keeping subtask \
                             Pending — scoped workers never receive foreign-scope nodes (T8 #650)"
                        );
                        continue; // skip — don't mark as Assigned
                    }
                    crate::worker_service::WorkerDispatchGate::NoVersionMatchedWorker {
                        workers,
                    } => {
                        tracing::warn!(
                            subtask_id = %st.id.0,
                            gateway_contract_version = uc_types::CONTRACT_VERSION,
                            worker_contract_versions = ?workers,
                            "No capability-matching worker declared the gateway contract_version, \
                             keeping subtask Pending — mixed-version cluster, upgrade workers in lockstep"
                        );
                        continue; // never dispatch silently
                    }
                }
                store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Assigned);
                // T12 #654: pick WHERE it goes first (soft preference — the
                // shared subject is the overflow default).
                let subject = resolve_dispatch_subject(&registry, st, &project_id, &sibling_hosts);
                dispatchable.push((st.clone(), subject));
            }
            drop(registry);
            (dispatchable, project_id)
        };

        if ready.is_empty() {
            return;
        }

        if let Some(nats_client) = &self.inner.nats_client {
            for (st, subject) in ready {
                // T10 #652: compose the dependency context from committed
                // graph outputs before publishing (None when no deps / no
                // graph plane — never a failed dispatch).
                let dep_ids: Vec<String> = st.depends_on.iter().map(|d| d.0.clone()).collect();
                let ctx = compose_context_block(&self.inner.task_store, task_id, &dep_ids).await;
                let execute = subtask_execute_payload(task_id, &st, &project_id, "", &[], ctx);
                // T4 #640: Nats-Msg-Id = idempotency_key activates the
                // stream's duplicate_window — re-sends collapse to one.
                let dedup = dispatch_dedup_headers(&execute.idempotency_key);
                match serde_json::to_vec(&execute) {
                    Ok(bytes) => {
                        if let Err(e) = nats_client
                            .publish_with_headers(subject.clone(), dedup, bytes.into())
                            .await
                        {
                            tracing::warn!(
                                error = %e,
                                subtask_id = %st.id.0,
                                dispatch_mode = ?st.dispatch_mode,
                                "Failed to publish subtask execute"
                            );
                            let mut store = self.inner.task_store.lock().await;
                            if st.dispatch_mode == uc_types::DispatchMode::Remote {
                                // Remote mode: increment retry, fail after 3
                                let new_retry = store
                                    .increment_dispatch_retry(task_id, &st.id.0)
                                    .unwrap_or(st.dispatch_retry_count + 1);
                                if new_retry >= 3 {
                                    tracing::error!(
                                        subtask_id = %st.id.0,
                                        retry_count = new_retry,
                                        "Remote dispatch failed after 3 retries, marking Failed"
                                    );
                                    store.update_subtask_status(
                                        task_id,
                                        &st.id.0,
                                        uc_types::SubtaskStatus::Failed,
                                    );
                                } else {
                                    store.update_subtask_status(
                                        task_id,
                                        &st.id.0,
                                        uc_types::SubtaskStatus::Pending,
                                    );
                                }
                            } else {
                                // PreferRemote: revert to Pending (existing behavior)
                                store.update_subtask_status(
                                    task_id,
                                    &st.id.0,
                                    uc_types::SubtaskStatus::Pending,
                                );
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            "Failed to serialize NatsSubtaskExecute"
                        );
                    }
                }
            }
        }
    }

    #[cfg(not(feature = "messaging"))]
    pub async fn publish_ready_subtasks(&self, _task_id: &str) {}
}

impl<E: EngineApi + Send + Sync + 'static> Clone for GrpcServer<E> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

// ── NATS subscriber (feature-gated) ─────────────────────────

/// Build NATS connect options that suppress the per-reconnect-attempt log flood.
///
/// Without an `event_callback`, async_nats logs every `Event::ClientError` at
/// INFO — when NATS is down, that's one line every ~4s for hours (observed:
/// 4+ hours of `client error: nats: IO error` drowning out real events). We
/// downgrade the chatty disconnect/error events to DEBUG and keep Connected at
/// INFO, plus cap reconnect backoff at 10s with jitter.
#[cfg(feature = "messaging")]
fn nats_connect_options() -> async_nats::ConnectOptions {
    async_nats::ConnectOptions::new()
        .event_callback(|event| async move {
            match event {
                async_nats::Event::Connected => {
                    tracing::info!("NATS reconnected");
                }
                async_nats::Event::Disconnected
                | async_nats::Event::ClientError(_)
                | async_nats::Event::ServerError(_) => {
                    // Chatty during outages — debug only to avoid log flooding.
                    tracing::debug!(event = %event, "NATS connection event");
                }
                other => {
                    tracing::info!(event = %other, "NATS connection event");
                }
            }
        })
        .reconnect_delay_callback(|attempts| {
            // Exponential backoff capped at 10s: 0.1s, 0.2s, 0.4s, ... 10s.
            let base = 100_u64.saturating_mul(2_u64.saturating_pow(attempts.min(20) as u32));
            std::time::Duration::from_millis(base.min(10_000))
        })
}

/// Connect to NATS with a bounded startup retry.
///
/// A one-shot connect silently degrades the server forever when NATS is
/// slow to boot (e.g. a docker-daemon restart races the gateway ahead of
/// the nats container — depends_on ordering does not apply to restart-
/// policy starts). Retrying here covers the startup race; once connected,
/// the client's own reconnect callbacks handle later outages. After the
/// retries are exhausted the caller proceeds in degraded mode, exactly
/// like the old one-shot behaviour.
#[cfg(feature = "messaging")]
async fn connect_nats_with_retry(nats_url: &str) -> Option<async_nats::Client> {
    const MAX_ATTEMPTS: u32 = 30;
    const RETRY_DELAY: std::time::Duration = std::time::Duration::from_secs(2);

    let mut last_err = None;
    for attempt in 1..=MAX_ATTEMPTS {
        match nats_connect_options().connect(nats_url).await {
            Ok(client) => {
                tracing::info!(nats_url = %nats_url, "Connected to NATS for TaskService");
                return Some(client);
            }
            Err(e) => {
                tracing::warn!(
                    nats_url = %nats_url,
                    attempt,
                    max_attempts = MAX_ATTEMPTS,
                    error = %e,
                    "NATS connect failed, retrying"
                );
                last_err = Some(e);
                tokio::time::sleep(RETRY_DELAY).await;
            }
        }
    }
    tracing::warn!(
        nats_url = %nats_url,
        error = ?last_err,
        "NATS unavailable after retries, TaskService will use local decomposition"
    );
    None
}

/// One observable input from the gateway's three NATS subscriptions or its
/// periodic snapshot-recovery timer.
///
/// Keeping stream termination as an explicit variant is important: a
/// `tokio::select!` branch written as `Some(message) = stream.next()` becomes
/// disabled when that stream closes. Because the snapshot interval never
/// closes, a surrounding `else` branch would then never run and the dead
/// subscription would never be recreated.
#[derive(Debug)]
#[cfg(any(feature = "messaging", test))]
enum NatsSubscriberInput<T> {
    SnapshotTick,
    TaskUpdate(T),
    TaskEvent(T),
    Heartbeat(T),
    SubscriptionEnded(&'static str),
}

/// Wait for the next subscriber input while preserving stream-closure as a
/// first-class lifecycle event. The generic stream seam lets unit tests use
/// in-memory streams while production uses `async_nats::Subscriber`.
#[cfg(any(feature = "messaging", test))]
async fn next_nats_subscriber_input<T, U, E, H>(
    snapshot_interval: &mut tokio::time::Interval,
    update_sub: &mut U,
    event_sub: &mut E,
    heartbeat_sub: &mut H,
) -> NatsSubscriberInput<T>
where
    U: futures::Stream<Item = T> + Unpin,
    E: futures::Stream<Item = T> + Unpin,
    H: futures::Stream<Item = T> + Unpin,
{
    use futures::StreamExt;

    tokio::select! {
        _ = snapshot_interval.tick() => NatsSubscriberInput::SnapshotTick,
        message = update_sub.next() => match message {
            Some(message) => NatsSubscriberInput::TaskUpdate(message),
            None => NatsSubscriberInput::SubscriptionEnded(NATS_SUBJECT_TASK_UPDATE),
        },
        message = event_sub.next() => match message {
            Some(message) => NatsSubscriberInput::TaskEvent(message),
            None => NatsSubscriberInput::SubscriptionEnded(NATS_SUBJECT_TASK_EVENT),
        },
        message = heartbeat_sub.next() => match message {
            Some(message) => NatsSubscriberInput::Heartbeat(message),
            None => NatsSubscriberInput::SubscriptionEnded(NATS_SUBJECT_HEARTBEAT),
        },
    }
}

/// Spawn a background task that subscribes to `uc.task.update`,
/// `uc.task.event`, and `uc.heartbeat`, updating the TaskStore accordingly,
/// while periodically requesting complete task snapshots for recovery.
#[cfg(feature = "messaging")]
fn spawn_nats_subscriber(
    nats_client: async_nats::Client,
    task_store: Arc<Mutex<TaskStore>>,
    worker_registry: Arc<RwLock<WorkerRegistry>>,
    event_tx: broadcast::Sender<TaskEvent>,
) {
    tokio::spawn(async move {
        // Resubscribe loop: if NATS drops (server restart, network blip), the
        // subscription streams end. Without this loop the subscriber task would
        // exit permanently — no more task updates/events/heartbeats would reach
        // the gRPC server, so heartbeat timeouts would mark InProgress tasks
        // Failed (600s) and WatchTask streams would go stale (OMP session
        // interruption). We re-subscribe instead of giving up.
        loop {
            // Subscribe to task updates
            let mut update_sub = match nats_client.subscribe(NATS_SUBJECT_TASK_UPDATE).await {
                Ok(sub) => sub,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Failed to subscribe to NATS task updates, retrying in 2s"
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    continue;
                }
            };

            // Subscribe to task events
            let mut event_sub = match nats_client.subscribe(NATS_SUBJECT_TASK_EVENT).await {
                Ok(sub) => sub,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Failed to subscribe to NATS task events, retrying in 2s"
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    continue;
                }
            };

            // Subscribe to heartbeats
            let mut heartbeat_sub = match nats_client.subscribe(NATS_SUBJECT_HEARTBEAT).await {
                Ok(sub) => sub,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Failed to subscribe to NATS heartbeats, retrying in 2s"
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    continue;
                }
            };

            tracing::info!("NATS subscriber started for TaskService");

            let mut snapshot_interval = tokio::time::interval(std::time::Duration::from_secs(30));
            snapshot_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

            loop {
                match next_nats_subscriber_input(
                    &mut snapshot_interval,
                    &mut update_sub,
                    &mut event_sub,
                    &mut heartbeat_sub,
                )
                .await
                {
                    NatsSubscriberInput::SnapshotTick => {
                        request_task_snapshots(&nats_client, &task_store).await;
                    }
                    NatsSubscriberInput::TaskUpdate(message) => {
                        match serde_json::from_slice::<NatsTaskUpdateEnvelope>(&message.payload) {
                            Ok(envelope) => {
                                let NatsTaskUpdateEnvelope {
                                    update,
                                    description,
                                    project_id,
                                } = envelope;
                                tracing::debug!(
                                    task_id = %update.task_id,
                                    status = %update.status,
                                    "Received NATS task update"
                                );
                                // Dedup: skip if this message_id was already processed
                                {
                                    let mut store = task_store.lock().await;
                                    if store.check_and_record_message_id(&update.message_id) {
                                        continue;
                                    }
                                }
                                {
                                    let mut store = task_store.lock().await;
                                    store.apply_update_with_metadata(
                                        &update,
                                        description.as_deref(),
                                        project_id.as_deref(),
                                    );
                                }

                                // Record events for subtask status transitions so
                                // WatchTask can broadcast them.
                                // We collect event data first, then record, to avoid
                                // borrow conflicts between immutable read and mutable write.
                                let events_to_record: Vec<uc_engine::AgentEventType> = {
                                    let store = task_store.lock().await;
                                    let mut events = Vec::new();
                                    if let Some(task) = store.tasks.get(&update.task_id) {
                                        for subtask_update in &update.subtasks {
                                            if let Some(subtask) = task
                                                .subtasks
                                                .iter()
                                                .find(|st| st.id.0 == subtask_update.subtask_id)
                                            {
                                                let event = match subtask.status {
                                                    uc_types::SubtaskStatus::Assigned => {
                                                        Some(uc_engine::AgentEventType::SubtaskAssigned {
                                                            task_id: task.id.clone(),
                                                            subtask_id: subtask.id.clone(),
                                                            worker_id: subtask.assigned_worker.clone().unwrap_or_default(),
                                                        })
                                                    }
                                                    uc_types::SubtaskStatus::InProgress => {
                                                        Some(uc_engine::AgentEventType::SubtaskStarted {
                                                            task_id: task.id.clone(),
                                                            subtask_id: subtask.id.clone(),
                                                            worker_id: subtask.assigned_worker.clone().unwrap_or_default(),
                                                        })
                                                    }
                                                    uc_types::SubtaskStatus::Completed => {
                                                        Some(uc_engine::AgentEventType::SubtaskCompleted {
                                                            task_id: task.id.clone(),
                                                            subtask_id: subtask.id.clone(),
                                                            summary: String::new(),
                                                            success: true,
                                                            modified_files: Vec::new(),
                                                            output: String::new(),
                                                            simulated: false,
                                                        })
                                                    }
                                                    uc_types::SubtaskStatus::Failed => {
                                                        Some(uc_engine::AgentEventType::SubtaskFailed {
                                                            task_id: task.id.clone(),
                                                            subtask_id: subtask.id.clone(),
                                                            error: String::new(),
                                                            recoverable: false,
                                                            stderr_tail: String::new(),
                                                            recent_tools: String::new(),
                                                        })
                                                    }
                                                    _ => None,
                                                };
                                                if let Some(e) = event {
                                                    events.push(e);
                                                }
                                            }
                                        }
                                    }
                                    events
                                };

                                // Build the proto events to broadcast directly from the
                                // collected events (not a slice of self.events): when the
                                // inline log is at capacity, record_event drains oldest
                                // entries, shifting newly-pushed events to lower indices —
                                // so events[event_count_before..] would return an empty
                                // slice and silently drop the broadcast.
                                let new_events: Vec<TaskEvent> =
                                    events_to_record.iter().cloned().map(|e| e.into()).collect();

                                // Record the collected events (consumes events_to_record)
                                {
                                    let mut store = task_store.lock().await;
                                    for e in events_to_record {
                                        store.record_event(e);
                                    }
                                }

                                // Broadcast to all WatchTask streams
                                for event in new_events {
                                    let _ = event_tx.send(event);
                                }

                                // Dispatch ready subtasks for this task
                                dispatch_ready_subtasks(
                                    &task_store,
                                    &worker_registry,
                                    &nats_client,
                                    &update.task_id,
                                )
                                .await;
                            }
                            Err(e) => {
                                tracing::warn!(
                                    error = %e,
                                    "Failed to parse NATS task update message"
                                );
                            }
                        }
                    }
                    NatsSubscriberInput::TaskEvent(message) => {
                        match serde_json::from_slice::<NatsTaskEvent>(&message.payload) {
                            Ok(nats_event) => {
                                tracing::debug!(
                                    event_type = %nats_event.r#type,
                                    task_id = %nats_event.task_id,
                                    "Received NATS task event"
                                );
                                // Dedup: skip if this message_id was already processed
                                {
                                    let mut store = task_store.lock().await;
                                    if store.check_and_record_message_id(&nats_event.message_id) {
                                        continue;
                                    }
                                }
                                // Convert NATS event to AgentEventType and record it
                                if let Some(agent_event) = nats_event_to_agent_event(&nats_event) {
                                    let proto_event: TaskEvent = agent_event.clone().into();
                                    let mut store = task_store.lock().await;
                                    store.record_event(agent_event);
                                    drop(store);
                                    // Broadcast to all WatchTask streams
                                    let _ = event_tx.send(proto_event);
                                }
                            }
                            Err(e) => {
                                tracing::warn!(
                                    error = %e,
                                    "Failed to parse NATS task event message"
                                );
                            }
                        }
                    }
                    NatsSubscriberInput::Heartbeat(message) => {
                        let mut store = task_store.lock().await;
                        store.update_last_heartbeat();
                        // Also track per-worker heartbeat for failover detection.
                        if let Ok(hb) = serde_json::from_slice::<NatsHeartbeat>(&message.payload) {
                            store.update_worker_heartbeat(
                                &hb.consumer_id,
                                hb.stale_dispatch_dropped,
                            );
                        }
                    }
                    NatsSubscriberInput::SubscriptionEnded(subject) => {
                        // Recreate the full subscription set. Keeping one stale
                        // stream while replacing another complicates ordering and
                        // can miss events across a reconnect boundary.
                        tracing::warn!(subject, "NATS subscription ended, re-subscribing in 2s");
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        break;
                    }
                }
            }
        }
    });
}

/// Apply a complete snapshot response from the Python Orchestrator.
///
/// Snapshot recovery is state-only: it may rebuild missing tasks, but it does
/// not dispatch subtasks. The Orchestrator remains the owner of execution and
/// will publish the next state transition through the normal update path.
#[cfg(any(feature = "messaging", test))]
fn apply_task_snapshot_response(
    store: &mut TaskStore,
    response: NatsTaskSnapshotResponse,
) -> usize {
    let mut rehydrated = 0;
    for envelope in response.tasks {
        let update = envelope.update;
        if update.partial {
            tracing::warn!(
                task_id = %update.task_id,
                "Ignoring partial task snapshot in recovery response"
            );
            continue;
        }

        let task_id = update.task_id.clone();
        if store.check_and_record_message_id(&update.message_id) {
            continue;
        }

        let was_known = store.tasks.contains_key(&task_id);
        store.apply_update_with_metadata(
            &update,
            envelope.description.as_deref(),
            envelope.project_id.as_deref(),
        );
        if !was_known && store.tasks.contains_key(&task_id) {
            rehydrated += 1;
        }
    }
    rehydrated
}

/// Request current complete task snapshots from the Python Orchestrator.
///
/// The request is best-effort and periodic. A gateway may start before the
/// Python consumer, so a missing responder is expected and retried on the
/// next interval. A successful response is applied through the same envelope
/// and TaskStore seam as live `uc.task.update` messages.
#[cfg(feature = "messaging")]
async fn request_task_snapshots(
    nats_client: &async_nats::Client,
    task_store: &Arc<Mutex<TaskStore>>,
) {
    let response = match tokio::time::timeout(
        std::time::Duration::from_secs(5),
        nats_client.request(
            NATS_SUBJECT_TASK_SNAPSHOT_REQUEST,
            b"{\"v\":1}".to_vec().into(),
        ),
    )
    .await
    {
        Ok(Ok(response)) => response,
        Ok(Err(error)) => {
            tracing::debug!(error = %error, "Task snapshot request had no responder");
            return;
        }
        Err(_) => {
            tracing::debug!("Task snapshot request timed out");
            return;
        }
    };

    let response = match serde_json::from_slice::<NatsTaskSnapshotResponse>(&response.payload) {
        Ok(response) => response,
        Err(error) => {
            tracing::warn!(error = %error, "Failed to parse task snapshot response");
            return;
        }
    };

    let mut store = task_store.lock().await;
    let rehydrated = apply_task_snapshot_response(&mut store, response);
    if rehydrated > 0 {
        tracing::info!(
            count = rehydrated,
            "Rehydrated tasks from snapshot response"
        );
    }
}

/// Spawn a background subscriber for `uc.file.changed` that incrementally
/// re-indexes changed files into the shared codebase index.
///
/// When a worker edits a file, it broadcasts the new content on
/// `uc.file.changed`. This subscriber calls `engine.reindex_file` so the
/// gateway's text/AST/semantic index reflects live edits across the cluster
/// without needing filesystem access to the worker's worktree.
///
/// Re-subscribes on NATS disconnect (same resilient pattern as
/// `spawn_nats_subscriber`).
#[cfg(feature = "messaging")]
fn spawn_file_changed_subscriber<E: EngineApi + Send + Sync + 'static>(
    nats_client: async_nats::Client,
    inner: Arc<GrpcServerInner<E>>,
) {
    use futures::StreamExt;

    tokio::spawn(async move {
        loop {
            let mut sub = match nats_client.subscribe(NATS_SUBJECT_FILE_CHANGED).await {
                Ok(sub) => sub,
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Failed to subscribe to uc.file.changed, retrying in 2s"
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    continue;
                }
            };

            tracing::info!("NATS subscriber started for uc.file.changed (index sync)");

            loop {
                tokio::select! {
                    Some(message) = sub.next() => {
                        match serde_json::from_slice::<NatsFileChanged>(&message.payload) {
                            Ok(fc) => {
                                if fc.content.is_empty()
                                    || fc.change_type == "deleted"
                                {
                                    // File deleted (or emptied) — remove its
                                    // symbols/embeddings from the shared index
                                    // so searches don't return stale hits.
                                    match inner.engine.delete_file_from_index(
                                        &fc.repo_id,
                                        &fc.file_path,
                                    ).await {
                                        Ok(()) => {
                                            tracing::info!(
                                                repo_id = %fc.repo_id,
                                                file_path = %fc.file_path,
                                                "Removed deleted file from index"
                                            );
                                        }
                                        Err(e) => {
                                            tracing::warn!(
                                                error = %e,
                                                repo_id = %fc.repo_id,
                                                file_path = %fc.file_path,
                                                "Failed to remove deleted file from index"
                                            );
                                        }
                                    }
                                    continue;
                                }
                                match inner.engine.reindex_file(
                                    &fc.repo_id,
                                    &fc.file_path,
                                    &fc.content,
                                ).await {
                                    Ok(resp) => {
                                        tracing::info!(
                                            repo_id = %fc.repo_id,
                                            file_path = %fc.file_path,
                                            symbols = resp.symbols_extracted,
                                            chunks = resp.chunks_embedded,
                                            "Re-indexed changed file"
                                        );
                                    }
                                    Err(e) => {
                                        tracing::warn!(
                                            error = %e,
                                            repo_id = %fc.repo_id,
                                            file_path = %fc.file_path,
                                            "Failed to re-index changed file"
                                        );
                                    }
                                }
                            }
                            Err(e) => {
                                tracing::warn!(
                                    error = %e,
                                    "Failed to parse uc.file.changed message"
                                );
                            }
                        }
                    }
                    else => {
                        tracing::warn!(
                            "uc.file.changed subscription ended, re-subscribing in 2s"
                        );
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        break;
                    }
                }
            }
        }
    });
}

/// Spawn a background task that periodically checks for heartbeat timeouts,
/// reaps stale attempts through the graph-plane timeout sweep, and marks
/// stale tasks as Failed.
/// D6 pause-grace width (T6 #642): seconds a paused task's still-RUNNING
/// attempts are allowed to finish before the authoritative side hard-stops
/// them. `UC_PAUSE_GRACE_SECS` overrides the 120s default (tests set it small
/// by passing the duration directly — this helper is the production path).
fn pause_grace_secs_from_env() -> u64 {
    std::env::var("UC_PAUSE_GRACE_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(120)
}

/// Arm the D6 pause-grace timer for a paused task (T6 #642). When the grace
/// window lapses and the task is STILL paused, every RUNNING graph attempt of
/// its graph is failed (`pause_grace_expired` — fence attempt, node back to
/// READY while the retry budget lasts) and each outcome is bridged back into
/// the legacy store. No re-dispatch happens here: the paused task's dispatch
/// gate (`get_ready_subtasks` requires InProgress) stays shut — the re-armed
/// nodes are picked up when the task resumes. Re-pausing a task replaces its
/// timer. Worker-side cooperative cancellation is T7 #643's seam.
/// T7 #643 — granular control-plane event on `uc.task.event`: the
/// subtask/node identity rides the `subtask_id` slot, extra detail
/// (attempt_id, reason, rearm state, ...) rides the `data` map. The
/// message_id is registered in the TaskStore dedup map so the gateway's
/// own NATS subscriber skips the echo of its own message.
#[cfg(feature = "messaging")]
async fn publish_task_control_event(
    nats_client: Option<&async_nats::Client>,
    task_store: &Arc<Mutex<TaskStore>>,
    task_id: &str,
    event_type: &str,
    subtask_id: Option<&str>,
    data: serde_json::Map<String, serde_json::Value>,
) {
    use std::time::{SystemTime, UNIX_EPOCH};
    let Some(nats_client) = nats_client else {
        return;
    };
    let ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    let message_id = format!(
        "{}:{}:{}::{}",
        task_id,
        event_type,
        subtask_id.unwrap_or("-"),
        ts_ms
    );
    let event = NatsTaskEvent {
        v: default_event_version(),
        message_id: Some(message_id.clone()),
        r#type: event_type.to_string(),
        task_id: task_id.to_string(),
        subtask_id: subtask_id.map(|s| s.to_string()),
        data,
    };
    match serde_json::to_vec(&event) {
        Ok(bytes) => {
            if let Err(e) = nats_client
                .publish(NATS_SUBJECT_TASK_EVENT.to_string(), bytes.into())
                .await
            {
                tracing::warn!(
                    error = %e,
                    event_type = %event_type,
                    "Failed to publish NATS task control event"
                );
            } else {
                let mut store = task_store.lock().await;
                store.check_and_record_message_id(&Some(message_id));
            }
        }
        Err(e) => {
            tracing::warn!(error = %e, "Failed to serialize NATS task control event");
        }
    }
}

/// NATS handle threaded into the pause-grace timer: a real client under
/// `messaging` (used to emit `attempt_cancelled` kill triggers), unit
/// under `not(messaging)` so the signature stays uniform.
#[cfg(feature = "messaging")]
type PauseGraceNats = Option<async_nats::Client>;
#[cfg(not(feature = "messaging"))]
type PauseGraceNats = ();

/// The "no NATS" value for [`PauseGraceNats`], uniform across feature
/// configurations (integration tests and non-messaging callers).
pub fn no_pause_grace_nats() -> PauseGraceNats {
    #[cfg(feature = "messaging")]
    {
        None
    }
    #[cfg(not(feature = "messaging"))]
    {}
}

pub fn spawn_pause_grace_timer(
    task_id: String,
    grace: std::time::Duration,
    task_store: Arc<Mutex<TaskStore>>,
    timers: Arc<std::sync::Mutex<HashMap<String, tokio::task::AbortHandle>>>,
    nats_client: PauseGraceNats,
) {
    // Replace any in-flight timer for this task.
    cancel_pause_grace_timer(&task_id, timers.clone());
    #[cfg(not(feature = "messaging"))]
    let _ = nats_client; // unit placeholder — no control plane to publish to
    let tid = task_id.clone();
    let handle = tokio::spawn(async move {
        tokio::time::sleep(grace).await;
        // Drop discipline: the tokio Mutex is not reentrant — check → sink →
        // bridge each hold its own lock acquisition.
        let still_paused = {
            let store = task_store.lock().await;
            store
                .get_task(&tid)
                .map(|t| t.status == uc_types::TaskStatus::Paused)
                .unwrap_or(false)
        };
        if !still_paused {
            return; // resumed before the grace lapsed — nothing to do
        }
        let swept: Vec<uc_engine::SweptAttempt> = {
            let store = task_store.lock().await;
            match store.graph_shadow() {
                Some(sink) => {
                    sink.fail_running_attempts(&tid, "pause_grace_expired")
                        .await
                }
                None => Vec::new(),
            }
        };
        if swept.is_empty() {
            return;
        }
        let bridged = {
            let mut store = task_store.lock().await;
            swept
                .iter()
                .filter(|s| store.revert_swept_subtask(&tid, &s.node_id, s.rearmed))
                .count()
        };
        tracing::warn!(
            task_id = %tid,
            grace_secs = grace.as_secs(),
            attempts_failed = swept.len(),
            subtasks_bridged = bridged,
            "Pause grace expired — running attempts hard-stopped via the graph plane"
        );
        // T7 #643 — the hard stop above fenced the attempts on the graph
        // rows, but the worker processes are still alive until they hit the
        // fence on their next report. Emit `attempt_cancelled` per swept
        // attempt so the worker kills the process immediately (cooperative
        // cancel, C3) instead of discovering the fence at commit time.
        #[cfg(feature = "messaging")]
        if let Some(client) = &nats_client {
            for s in &swept {
                let mut data = serde_json::Map::new();
                data.insert(
                    "attempt_id".to_string(),
                    serde_json::Value::String(s.attempt_id.clone()),
                );
                data.insert("rearmed".to_string(), serde_json::Value::Bool(s.rearmed));
                data.insert(
                    "reason".to_string(),
                    serde_json::Value::String("pause_grace_expired".to_string()),
                );
                publish_task_control_event(
                    Some(client),
                    &task_store,
                    &tid,
                    "attempt_cancelled",
                    Some(&s.node_id),
                    data,
                )
                .await;
            }
        }
    });
    if let Ok(mut map) = timers.lock() {
        map.insert(task_id, handle.abort_handle());
    }
}

/// Cancel (and forget) a task's pause-grace timer. Called on resume — and
/// defensively before re-arming on a second pause.
pub fn cancel_pause_grace_timer(
    task_id: &str,
    timers: Arc<std::sync::Mutex<HashMap<String, tokio::task::AbortHandle>>>,
) {
    if let Ok(mut map) = timers.lock() {
        if let Some(handle) = map.remove(task_id) {
            handle.abort();
        }
    }
}

#[cfg(feature = "messaging")]
fn spawn_heartbeat_monitor(
    nats_client: async_nats::Client,
    task_store: Arc<Mutex<TaskStore>>,
    worker_registry: Arc<RwLock<WorkerRegistry>>,
    heartbeat_timeout: std::time::Duration,
) {
    tokio::spawn(async move {
        // Check every 30 seconds
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));

        loop {
            interval.tick().await;

            let stale_workers = {
                let mut store = task_store.lock().await;
                let failed = store.mark_stale_tasks_failed(heartbeat_timeout);
                if !failed.is_empty() {
                    tracing::warn!(
                        task_ids = ?failed,
                        "Marked tasks as Failed due to heartbeat timeout"
                    );
                }

                // Worker staleness now only drives registry hygiene — the
                // subtask-level fallout is owned by the graph-plane sweep
                // below (T6 #642), which replaces both legacy reassign
                // reapers (dead-worker AND Assigned-never-picked-up: the
                // attempt row is created RUNNING at schedule time, so
                // `COALESCE(heartbeat_at, started_at)` ages past the sweep
                // window in either case).
                store.mark_stale_workers(heartbeat_timeout)
                // store dropped here — tokio::sync::Mutex is not reentrant,
                // and the sweep + bridge below re-acquire it.
            };

            // Remove stale workers from the registry so listWorkers stops
            // reporting them (is_available=false). Without this, a worker
            // that went silent stays in the registry forever, polluting the
            // dashboard and polluting capability queries.
            if !stale_workers.is_empty() {
                let mut registry = worker_registry.write().await;
                for wid in &stale_workers {
                    let _ = registry.deregister(wid);
                }
                tracing::info!(
                    worker_ids = ?stale_workers,
                    "Removed stale workers from registry (subtask fallout owned by the graph sweep)"
                );
            }

            // Graph-plane reaper (T6 #642): sweep stale RUNNING attempts and
            // bridge the outcomes back into the legacy store, then
            // re-dispatch the affected tasks so live workers pick the
            // re-armed nodes up. With no graph shadow wired (shadow mode
            // off) the sweep is a no-op — the legacy monitor semantics end
            // here, which is the documented T6 posture.
            let swept: Vec<uc_engine::SweptAttempt> = {
                let store = task_store.lock().await;
                match store.graph_shadow() {
                    Some(sink) => sink.sweep_timeouts(heartbeat_timeout).await,
                    None => Vec::new(),
                }
            };
            if !swept.is_empty() {
                let mut affected: Vec<String> = Vec::new();
                {
                    let mut store = task_store.lock().await;
                    for s in &swept {
                        if store.revert_swept_subtask(&s.graph_id, &s.node_id, s.rearmed)
                            && !affected.contains(&s.graph_id)
                        {
                            affected.push(s.graph_id.clone());
                        }
                    }
                }
                tracing::warn!(
                    swept = swept.len(),
                    tasks_affected = affected.len(),
                    "Graph-plane timeout sweep reaped stale attempts"
                );
                for task_id in &affected {
                    dispatch_ready_subtasks(&task_store, &worker_registry, &nats_client, task_id)
                        .await;
                }
            }
        }
    });
}

/// Dispatch ready subtasks for a task by publishing them to `uc.subtask.execute`.
///
/// Called by the NATS subscriber after processing a `uc.task.update` —
/// completing a subtask may unblock dependents — and by the heartbeat
/// monitor after a graph-plane sweep bridge re-armed nodes (T6 #642).
#[cfg(feature = "messaging")]
async fn dispatch_ready_subtasks(
    task_store: &Arc<Mutex<TaskStore>>,
    worker_registry: &Arc<RwLock<WorkerRegistry>>,
    nats_client: &async_nats::Client,
    task_id: &str,
) {
    let (ready, project_id) = {
        let mut store = task_store.lock().await;
        let subtasks = store.get_ready_subtasks(task_id);
        let project_id = store
            .get_task(task_id)
            .map(|t| t.project_id.clone())
            .unwrap_or_default();
        // Capability-aware dispatch (mirrors publish_ready_subtasks): only mark
        // as Assigned if a matching worker exists. Without this, a subtask with
        // unmet required_capabilities was marked Assigned and published, then the
        // worker rejected it — but it stayed Assigned forever (get_ready_subtasks
        // only returns Pending), stalling the task.
        let registry = worker_registry.read().await;
        // T12 #654: same locality input as publish_ready_subtasks (the store
        // is already locked here — sibling_worker_hosts only reads it).
        let sibling_hosts = sibling_worker_hosts(&store, &registry, task_id);
        let mut dispatchable = Vec::new();
        for st in &subtasks {
            // Cap dispatch retries: if a subtask has been reverted to Pending
            // this many times (publish failures, or the graph-plane sweep
            // re-dispatch path), mark Failed to stop an
            // infinite dispatch storm (publish → Assigned → revert → publish,
            // every 30s). Mirrors the Remote-mode publish-failure cap of 3.
            if st.dispatch_retry_count >= 3 {
                tracing::error!(
                    subtask_id = %st.id.0,
                    retry_count = st.dispatch_retry_count,
                    "Subtask exceeded max dispatch retries (no worker picked it up), marking Failed"
                );
                store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Failed);
                continue;
            }
            // Capability + scope + contract_version hard gate (mirrors
            // publish_ready_subtasks, T1 #637; scope filter T8 #650): never
            // dispatch silently to a worker that has not confirmed the
            // gateway's contract version or does not serve the task's scope.
            match registry.dispatch_gate(&st.required_capabilities, &project_id) {
                crate::worker_service::WorkerDispatchGate::Dispatch => {}
                crate::worker_service::WorkerDispatchGate::NoCapableWorker => {
                    tracing::info!(
                        subtask_id = %st.id.0,
                        required_capabilities = ?st.required_capabilities,
                        "No worker with matching capabilities, keeping subtask Pending"
                    );
                    continue; // skip — don't mark as Assigned
                }
                crate::worker_service::WorkerDispatchGate::NoScopeMatchedWorker { workers } => {
                    tracing::info!(
                        subtask_id = %st.id.0,
                        project_id = %project_id,
                        scope_capable_workers = ?workers,
                        "No scope-matching worker serves this project_id, keeping subtask \
                         Pending — scoped workers never receive foreign-scope nodes (T8 #650)"
                    );
                    continue; // skip — don't mark as Assigned
                }
                crate::worker_service::WorkerDispatchGate::NoVersionMatchedWorker { workers } => {
                    tracing::warn!(
                        subtask_id = %st.id.0,
                        gateway_contract_version = uc_types::CONTRACT_VERSION,
                        worker_contract_versions = ?workers,
                        "No capability-matching worker declared the gateway contract_version, \
                         keeping subtask Pending — mixed-version cluster, upgrade workers in lockstep"
                    );
                    continue;
                }
            }
            store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Assigned);
            // T12 #654: affinity placement target (shared subject = overflow).
            let subject = resolve_dispatch_subject(&registry, st, &project_id, &sibling_hosts);
            dispatchable.push((st.clone(), subject));
        }
        (dispatchable, project_id)
    };

    for (st, subject) in ready {
        // Propagate expected_output so the worker's prompt includes the
        // actual success criteria (not the generic fallback). Matches the
        // gRPC upsert path. Propagate file_constraints so the worker can do
        // conflict detection and workspace isolation (empty used to defeat
        // both — concurrent subtasks sharing files would race and
        // corrupt/merge-conflict).
        // T10 #652: dependency context composed from committed graph outputs
        // (None when no deps / no graph plane — never a failed dispatch).
        let dep_ids: Vec<String> = st.depends_on.iter().map(|d| d.0.clone()).collect();
        let ctx = compose_context_block(task_store, task_id, &dep_ids).await;
        let execute = subtask_execute_payload(
            task_id,
            &st,
            &project_id,
            &st.expected_output,
            &st.file_constraints,
            ctx,
        );
        // T4 #640: same dedup header on the second dispatch mouth.
        let dedup = dispatch_dedup_headers(&execute.idempotency_key);
        match serde_json::to_vec(&execute) {
            Ok(bytes) => {
                if let Err(e) = nats_client
                    .publish_with_headers(subject.clone(), dedup, bytes.into())
                    .await
                {
                    tracing::warn!(
                        error = %e,
                        subtask_id = %st.id.0,
                        dispatch_mode = ?st.dispatch_mode,
                        "Failed to publish subtask execute (dispatch_ready_subtasks)"
                    );
                    let mut store = task_store.lock().await;
                    if st.dispatch_mode == uc_types::DispatchMode::Remote {
                        // Remote mode: increment retry, fail after 3
                        let new_retry = store
                            .increment_dispatch_retry(task_id, &st.id.0)
                            .unwrap_or(st.dispatch_retry_count + 1);
                        if new_retry >= 3 {
                            tracing::error!(
                                subtask_id = %st.id.0,
                                retry_count = new_retry,
                                "Remote dispatch failed after 3 retries, marking Failed"
                            );
                            store.update_subtask_status(
                                task_id,
                                &st.id.0,
                                uc_types::SubtaskStatus::Failed,
                            );
                        } else {
                            store.update_subtask_status(
                                task_id,
                                &st.id.0,
                                uc_types::SubtaskStatus::Pending,
                            );
                        }
                    } else {
                        // PreferRemote: revert to Pending (existing behavior)
                        store.update_subtask_status(
                            task_id,
                            &st.id.0,
                            uc_types::SubtaskStatus::Pending,
                        );
                    }
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, "Failed to serialize NatsSubtaskExecute");
            }
        }
    }
}

/// Convert a `NatsTaskEvent` to an `AgentEventType`.
///
/// Returns None for unrecognized event types.
#[cfg(feature = "messaging")]
fn nats_event_to_agent_event(event: &NatsTaskEvent) -> Option<uc_engine::AgentEventType> {
    match event.r#type.as_str() {
        "subtask_assigned" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let worker_id = event
                .data
                .get("worker_id")
                .and_then(|v| v.as_str())
                .map(|s| uc_types::WorkerId(s.to_string()))
                .unwrap_or_default();
            Some(uc_engine::AgentEventType::SubtaskAssigned {
                task_id,
                subtask_id,
                worker_id,
            })
        }
        "subtask_started" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let worker_id = event
                .data
                .get("worker_id")
                .and_then(|v| v.as_str())
                .map(|s| uc_types::WorkerId(s.to_string()))
                .unwrap_or_default();
            Some(uc_engine::AgentEventType::SubtaskStarted {
                task_id,
                subtask_id,
                worker_id,
            })
        }
        "tool_call" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let tool_name = event
                .data
                .get("tool_name")
                .or_else(|| event.data.get("tool"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let tool_input = event
                .data
                .get("tool_input")
                .or_else(|| event.data.get("input_summary"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Some(uc_engine::AgentEventType::ToolInvoked {
                task_id,
                subtask_id,
                tool_name,
                tool_input,
            })
        }
        "tool_result" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let tool_output = event
                .data
                .get("tool_output")
                .or_else(|| event.data.get("result_summary"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let success = json_bool_or_default(&event.data, "success", true);
            Some(uc_engine::AgentEventType::ToolResult {
                task_id,
                subtask_id,
                tool_output,
                success,
            })
        }
        "file_modified" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let file_path = event
                .data
                .get("file_path")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let diff = event
                .data
                .get("diff")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Some(uc_engine::AgentEventType::FileModified {
                task_id,
                subtask_id,
                file_path,
                diff,
            })
        }
        "subtask_completed" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let summary = event
                .data
                .get("summary")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let success = json_bool_or_default(&event.data, "success", true);
            let output = event
                .data
                .get("output")
                .or_else(|| event.data.get("result"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Some(uc_engine::AgentEventType::SubtaskCompleted {
                task_id,
                subtask_id,
                summary,
                success,
                modified_files: Vec::new(),
                output,
                simulated: false,
            })
        }
        "subtask_failed" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let error = event
                .data
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("Unknown error")
                .to_string();
            let recoverable = json_bool_or_default(&event.data, "recoverable", false);
            let stderr_tail = event
                .data
                .get("stderr_tail")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let recent_tools = event
                .data
                .get("recent_tools")
                .map(|v| {
                    // recent_tools may arrive as a JSON array or a string
                    if v.is_string() {
                        v.as_str().unwrap_or("").to_string()
                    } else {
                        // Serialize array or other value as JSON string
                        serde_json::to_string(v).unwrap_or_default()
                    }
                })
                .unwrap_or_default();
            Some(uc_engine::AgentEventType::SubtaskFailed {
                task_id,
                subtask_id,
                error,
                recoverable,
                stderr_tail,
                recent_tools,
            })
        }
        "subtask_progress" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            let subtask_id = uc_types::TaskId(event.subtask_id.clone().unwrap_or_default());
            let worker_id = event
                .data
                .get("worker_id")
                .and_then(|v| v.as_str())
                .map(|s| uc_types::WorkerId(s.to_string()))
                .unwrap_or_default();
            let phase = event
                .data
                .get("phase")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            // percent may arrive as a number or a stringified number.
            let percent = event
                .data
                .get("percent")
                .and_then(|v| {
                    v.as_u64()
                        .or_else(|| v.as_i64().map(|i| i as u64))
                        .or_else(|| v.as_str().and_then(|s| s.parse::<u64>().ok()))
                })
                .unwrap_or(0) as u32;
            let step_index = event
                .data
                .get("step_index")
                .and_then(|v| {
                    v.as_u64()
                        .or_else(|| v.as_str().and_then(|s| s.parse::<u64>().ok()))
                })
                .map(|n| n as u32);
            let step_total = event
                .data
                .get("step_total")
                .and_then(|v| {
                    v.as_u64()
                        .or_else(|| v.as_str().and_then(|s| s.parse::<u64>().ok()))
                })
                .map(|n| n as u32);
            let step_agent = event
                .data
                .get("step_agent")
                .and_then(|v| v.as_str())
                .map(String::from);
            let step_status = event
                .data
                .get("step_status")
                .and_then(|v| v.as_str())
                .map(String::from);
            let step_summary = event
                .data
                .get("step_summary")
                .and_then(|v| v.as_str())
                .map(String::from);
            let parallel_group = event
                .data
                .get("parallel_group")
                .and_then(|v| v.as_str())
                .map(String::from);
            let parallel_step_count = event
                .data
                .get("parallel_step_count")
                .and_then(|v| {
                    v.as_u64()
                        .or_else(|| v.as_str().and_then(|s| s.parse::<u64>().ok()))
                })
                .map(|n| n as u32);
            Some(uc_engine::AgentEventType::SubtaskProgress {
                task_id,
                subtask_id,
                worker_id,
                phase,
                percent,
                step_index,
                step_total,
                step_agent,
                step_status,
                step_summary,
                parallel_group,
                parallel_step_count,
            })
        }
        "task_paused" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            Some(uc_engine::AgentEventType::TaskPaused { task_id })
        }
        "task_resumed" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            Some(uc_engine::AgentEventType::TaskResumed { task_id })
        }
        "task_cancelled" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            Some(uc_engine::AgentEventType::TaskCancelled { task_id })
        }
        // T4 #640 / D7: a worker term-dropped an old-envelope dispatch left
        // in the stream (legacy publisher / upgrade window). Surfaced as a
        // task-level event so the drop is user-visible, not just a log line.
        "stale_dispatch_dropped" => {
            let task_id = uc_types::TaskId(event.task_id.clone());
            Some(uc_engine::AgentEventType::TaskUpdated {
                task_id,
                status: "stale_dispatch_dropped".to_string(),
            })
        }
        _ => {
            tracing::debug!(
                event_type = %event.r#type,
                "Ignoring unrecognized NATS event type"
            );
            None
        }
    }
}

fn to_status(err: uc_types::EngineError) -> Status {
    use uc_types::EngineError::*;
    let (code, msg) = match &err {
        NotFound(m) => (tonic::Code::NotFound, m.clone()),
        SearchError(m) => (tonic::Code::Internal, m.clone()),
        IndexError(m) => (tonic::Code::NotFound, m.clone()),
        MemoryReadError(m) | MemoryWriteError(m) => (tonic::Code::Internal, m.clone()),
        IndexingError(m) => (tonic::Code::Internal, m.clone()),
        ConnectionError(m) => (tonic::Code::Unavailable, m.clone()),
        TimeoutError(m) => (tonic::Code::DeadlineExceeded, m.clone()),
        RateLimited(secs) => (
            tonic::Code::ResourceExhausted,
            format!("retry after {}s", secs),
        ),
        ConflictError { path, details } => (tonic::Code::Aborted, format!("{}: {}", path, details)),
        TaskError(m) => (tonic::Code::FailedPrecondition, m.clone()),
        WorkerUnavailable(m) => (tonic::Code::Unavailable, m.clone()),
        SandboxError(m) => (tonic::Code::PermissionDenied, m.clone()),
        ConfigError(m) => (tonic::Code::InvalidArgument, m.clone()),
        InternalError(m) => (tonic::Code::Internal, m.clone()),
        StorageError(m) => (tonic::Code::Unavailable, m.clone()),
        InvalidOperation(m) => (tonic::Code::FailedPrecondition, m.clone()),
    };
    Status::new(code, msg)
}

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> EngineService for GrpcServer<E> {
    async fn search(
        &self,
        request: Request<SearchRequest>,
    ) -> Result<Response<SearchResponse>, Status> {
        let req = request.into_inner();
        let query: uc_types::SearchQuery = req.into();
        let result = self.inner.engine.search(query).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn index_repo(
        &self,
        request: Request<IndexRepoRequest>,
    ) -> Result<Response<IndexRepoResponse>, Status> {
        let req = request.into_inner();
        let index_req: uc_types::IndexRequest = req.into();
        let result = self
            .inner
            .engine
            .index_repo(index_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn get_index_state(
        &self,
        request: Request<GetIndexStateRequest>,
    ) -> Result<Response<GetIndexStateResponse>, Status> {
        let req = request.into_inner();
        let repo_id = req.repo_id.clone();
        let result = self
            .inner
            .engine
            .get_index_state(&repo_id)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn remove_index(
        &self,
        request: Request<RemoveIndexRequest>,
    ) -> Result<Response<RemoveIndexResponse>, Status> {
        let req = request.into_inner();
        self.inner
            .engine
            .remove_index(&req.repo_id)
            .await
            .map_err(to_status)?;
        Ok(Response::new(RemoveIndexResponse {}))
    }

    async fn read_memory(
        &self,
        request: Request<ReadMemoryRequest>,
    ) -> Result<Response<ReadMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        let read_req = uc_types::MemoryReadRequest {
            key,
            include_semantic: req.include_semantic,
        };
        let result = self
            .inner
            .engine
            .read_memory(read_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn write_memory(
        &self,
        request: Request<WriteMemoryRequest>,
    ) -> Result<Response<WriteMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        let content = match req.content_type.as_str() {
            "structured" => uc_types::MemoryContent::Structured(
                serde_json::from_str(&req.content)
                    .unwrap_or(serde_json::Value::String(req.content.clone())),
            ),
            "code" => uc_types::MemoryContent::Code {
                language: req.language.unwrap_or_default(),
                code: req.content,
            },
            "diff" => uc_types::MemoryContent::Diff {
                file_path: req.file_path.unwrap_or_default(),
                diff: req.content,
            },
            "reference" => uc_types::MemoryContent::Reference {
                uri: req.uri.unwrap_or_default(),
                description: req.description.unwrap_or_default(),
            },
            _ => uc_types::MemoryContent::Text(req.content),
        };
        let write_req = uc_types::MemoryWriteRequest {
            key,
            content,
            metadata: uc_types::MemoryMetadata {
                source_agent: req.source_agent,
                importance: req.importance,
                tags: req.tags,
                embedding: None,
            },
            version: req.version.filter(|&v| v != 0),
        };
        let result = self
            .inner
            .engine
            .write_memory(write_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn replay_memory_write(
        &self,
        request: Request<ReplayMemoryWriteRequest>,
    ) -> Result<Response<ReplayMemoryWriteResponse>, Status> {
        let req = request.into_inner();
        let write_req: uc_types::MemoryWriteRequest = req.into();
        let result = self
            .inner
            .engine
            .replay_memory_write(write_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn delete_memory(
        &self,
        request: Request<DeleteMemoryRequest>,
    ) -> Result<Response<DeleteMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        self.inner
            .engine
            .delete_memory(&key)
            .await
            .map_err(to_status)?;
        Ok(Response::new(DeleteMemoryResponse {}))
    }

    async fn search_memory(
        &self,
        request: Request<SearchMemoryRequest>,
    ) -> Result<Response<SearchMemoryResponse>, Status> {
        let req = request.into_inner();
        let search_req: uc_types::MemorySearchRequest = req.into();
        let result = self
            .inner
            .engine
            .search_memory(search_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let result = self.inner.engine.health().await.map_err(to_status)?;

        Ok(Response::new(result.into()))
    }

    #[allow(clippy::result_large_err)]
    async fn batch_write_memory(
        &self,
        request: Request<BatchWriteMemoryRequest>,
    ) -> Result<Response<BatchWriteMemoryResponse>, Status> {
        let proto = request.into_inner();
        #[allow(clippy::result_large_err)]
        let write_requests: Vec<uc_types::MemoryWriteRequest> = proto
            .requests
            .into_iter()
            .map(|req| -> Result<uc_types::MemoryWriteRequest, Status> {
                let key_scope = req.key_scope.as_str();
                if !matches!(key_scope, "task" | "project" | "global") {
                    return Err(Status::invalid_argument(format!(
                        "Invalid key_scope: '{}'. Must be 'task', 'project', or 'global'",
                        key_scope
                    )));
                }
                if key_scope == "task" && req.task_id.is_empty() {
                    return Err(Status::invalid_argument(
                        "task_id is required for task-scoped memory",
                    ));
                }
                if key_scope == "project" && req.project_id.is_empty() {
                    return Err(Status::invalid_argument(
                        "project_id is required for project-scoped memory",
                    ));
                }
                Ok(req.into())
            })
            .collect::<Result<Vec<_>, Status>>()?;
        let entries = self
            .inner
            .engine
            .batch_write_memory(write_requests)
            .await
            .map_err(to_status)?;
        let response = BatchWriteMemoryResponse {
            entries: entries.into_iter().map(Into::into).collect(),
        };
        Ok(Response::new(response))
    }

    async fn list_repos(
        &self,
        request: Request<ListReposRequest>,
    ) -> Result<Response<ListReposResponse>, Status> {
        let req = request.into_inner();
        let repos = self
            .inner
            .engine
            .list_repos(req.workspace_id.as_deref())
            .await
            .map_err(to_status)?;
        let response = ListReposResponse {
            repos: repos.into_iter().map(Into::into).collect(),
        };
        Ok(Response::new(response))
    }

    async fn list_dir(
        &self,
        request: Request<ListDirRequest>,
    ) -> Result<Response<ListDirResponse>, Status> {
        let req = request.into_inner();
        let listing = self
            .inner
            .engine
            .list_dir(&req.repo_id, &req.path)
            .await
            .map_err(to_status)?;
        Ok(Response::new(listing.into()))
    }

    async fn get_file(
        &self,
        request: Request<GetFileRequest>,
    ) -> Result<Response<GetFileResponse>, Status> {
        let req = request.into_inner();
        let file = self
            .inner
            .engine
            .get_file(&req.repo_id, &req.path)
            .await
            .map_err(to_status)?;
        Ok(Response::new(file.into()))
    }

    type SearchStreamStream = std::pin::Pin<
        Box<dyn tokio_stream::Stream<Item = Result<SearchResultItem, Status>> + Send>,
    >;

    async fn search_stream(
        &self,
        request: Request<SearchStreamRequest>,
    ) -> Result<Response<Self::SearchStreamStream>, Status> {
        let proto = request.into_inner();
        let query: uc_types::SearchQuery = proto.into();
        let stream = self
            .inner
            .engine
            .search_stream(query)
            .await
            .map_err(to_status)?;
        // Flatten: each SearchResult contains multiple items, but the
        // proto stream sends one SearchResultItem at a time.
        use futures::StreamExt;
        let flattened = stream.flat_map(|result| {
            let items: Vec<SearchResultItem> = result.items.into_iter().map(Into::into).collect();
            tokio_stream::iter(items.into_iter().map(Ok::<_, Status>))
        });
        Ok(Response::new(Box::pin(flattened)))
    }
}

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> TaskService for GrpcServer<E> {
    async fn submit_task(
        &self,
        request: Request<SubmitTaskRequest>,
    ) -> Result<Response<SubmitTaskResponse>, Status> {
        let req = request.into_inner();

        if req.description.is_empty() {
            return Ok(Response::new(SubmitTaskResponse {
                success: false,
                task_id: String::new(),
                status: String::new(),
                subtask_count: 0,
                subtasks: Vec::new(),
                error: Some("Task description cannot be empty".to_string()),
            }));
        }

        // ExecutionScope (T8 #650 / D8 #645): project_id is mandatory and
        // immutable after creation — reject empty/whitespace at the gate so
        // every task is born with a concrete scope.
        if req.project_id.trim().is_empty() {
            return Ok(Response::new(SubmitTaskResponse {
                success: false,
                task_id: String::new(),
                status: String::new(),
                subtask_count: 0,
                subtasks: Vec::new(),
                error: Some(
                    "project_id cannot be empty — every task must declare a project scope \
                     (D8 #645)"
                        .to_string(),
                ),
            }));
        }

        #[cfg(feature = "messaging")]
        {
            // Try NATS publish first
            if let Some(nats_client) = &self.inner.nats_client {
                // Create task in Planning status and extract the data we need,
                // then release the lock BEFORE the async NATS publish.
                // Holding the lock across an async publish would block all
                // other TaskStore operations (get_task, list_tasks, etc.).
                let (task_id_str, submit_payload, new_events) = {
                    let mut store = self.inner.task_store.lock().await;
                    let (task, events) =
                        store.submit_task_pending(req.description.clone(), req.project_id.clone());

                    let payload = NatsTaskSubmit {
                        task_id: task.id.0.clone(),
                        description: req.description.clone(),
                        project_id: req.project_id.clone(),
                        // Real-time gRPC submission — no `scheduled` flag.
                        // The Python consumer treats absent as `False`
                        // (subject to night-window deferral).
                        scheduled: None,
                        verify_command: None,
                    };
                    (task.id.0.clone(), payload, events)
                };
                // Broadcast TaskCreated event to WatchTask streams
                for event in new_events {
                    let _ = self.inner.event_tx.send(event);
                }

                let payload_bytes = match serde_json::to_vec(&submit_payload) {
                    Ok(bytes) => bytes,
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            "Failed to serialize NATS submit payload"
                        );
                        // Remove the Planning placeholder
                        {
                            let mut store = self.inner.task_store.lock().await;
                            store.tasks.remove(&task_id_str);
                            store.events.retain(|e| {
                                !matches!(e, uc_engine::AgentEventType::TaskCreated { task_id, .. } if task_id.0 == task_id_str)
                            });
                        }
                        return Ok(Response::new(SubmitTaskResponse {
                            success: false,
                            task_id: String::new(),
                            status: String::new(),
                            subtask_count: 0,
                            subtasks: Vec::new(),
                            error: Some(format!("NATS payload serialization failed: {e}")),
                        }));
                    }
                };

                match nats_client
                    .publish(NATS_SUBJECT_TASK_SUBMIT.to_string(), payload_bytes.into())
                    .await
                {
                    Ok(()) => {
                        tracing::info!(
                            task_id = %task_id_str,
                            "Task submitted via NATS, awaiting Python Orchestrator"
                        );
                        // Read the task back to build the response
                        let store = self.inner.task_store.lock().await;
                        let task = store.get_task(&task_id_str).expect("task just inserted");
                        let subtask_protos: Vec<SubtaskProto> =
                            task.subtasks.clone().into_iter().map(Into::into).collect();

                        return Ok(Response::new(SubmitTaskResponse {
                            success: true,
                            task_id: task.id.0.clone(),
                            status: task_status_to_proto(&task.status).to_string(),
                            subtask_count: task.subtasks.len() as u32,
                            subtasks: subtask_protos,
                            error: None,
                        }));
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            "NATS publish failed, falling back to local worker bridge"
                        );
                        // Remove the Planning placeholder created for NATS path
                        {
                            let mut store = self.inner.task_store.lock().await;
                            store.tasks.remove(&task_id_str);
                            store.events.retain(|e| {
                                !matches!(e, uc_engine::AgentEventType::TaskCreated { task_id, .. } if task_id.0 == task_id_str)
                            });
                        }
                        // Fall through to local worker bridge
                    }
                }
            }
        }

        // No NATS or NATS publish failed — local fallback
        // Create the task locally (same as NATS path) but mark it as
        // locally submitted. The TypeScript orchestrator will decompose
        // locally and execute subtasks with prefer_remote dispatch.
        tracing::info!(
            description = %req.description,
            "Task submitted locally (NATS unavailable), orchestrator will handle decomposition"
        );
        let (task_id_str, new_events) = {
            let mut store = self.inner.task_store.lock().await;
            let (task, events) =
                store.submit_task_pending(req.description.clone(), req.project_id.clone());
            (task.id.0.clone(), events)
        };
        // Broadcast TaskCreated event to WatchTask streams
        for event in new_events {
            let _ = self.inner.event_tx.send(event);
        }

        let store = self.inner.task_store.lock().await;
        let task = store.get_task(&task_id_str).expect("task just inserted");
        let subtask_protos: Vec<SubtaskProto> =
            task.subtasks.clone().into_iter().map(Into::into).collect();

        Ok(Response::new(SubmitTaskResponse {
            success: true,
            task_id: task.id.0.clone(),
            status: task_status_to_proto(&task.status).to_string(),
            subtask_count: task.subtasks.len() as u32,
            subtasks: subtask_protos,
            error: None,
        }))
    }

    async fn get_task(
        &self,
        request: Request<GetTaskRequest>,
    ) -> Result<Response<GetTaskResponse>, Status> {
        let req = request.into_inner();
        let store = self.inner.task_store.lock().await;
        match store.get_task(&req.task_id) {
            Some(task) => Ok(Response::new(GetTaskResponse {
                available: true,
                task: Some(task.clone().into()),
            })),
            None => Ok(Response::new(GetTaskResponse {
                available: false,
                task: None,
            })),
        }
    }

    async fn list_tasks(
        &self,
        _request: Request<ListTasksRequest>,
    ) -> Result<Response<ListTasksResponse>, Status> {
        let store = self.inner.task_store.lock().await;
        let tasks: Vec<TaskProto> = store.list_tasks().into_iter().map(Into::into).collect();
        let total = tasks.len() as u32;

        // Compute status counts
        let mut status_counts: HashMap<String, u32> = HashMap::new();
        for task in &tasks {
            *status_counts.entry(task.status.clone()).or_insert(0) += 1;
        }

        Ok(Response::new(ListTasksResponse {
            available: true,
            tasks,
            total,
            status_counts,
        }))
    }

    type WatchTaskStream =
        std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<TaskEvent, Status>> + Send>>;

    async fn watch_task(
        &self,
        request: Request<WatchTaskRequest>,
    ) -> Result<Response<Self::WatchTaskStream>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id;
        let task_store = self.inner.task_store.clone();
        // Subscribe to broadcast before reading TaskStore. This eliminates the
        // race where events published between Phase 1 (replay) and Phase 2 (live)
        // would be missed. Since subscribe() happens here (before the stream
        // starts iterating), any event published after this point is buffered
        // by the broadcast channel and will be received in Phase 2.
        let event_rx = self.inner.event_tx.subscribe();

        let stream = async_stream::stream! {
            // Phase 1: replay existing events from TaskStore.
            // When taskId is empty (TUI "watch all"), skip replay entirely.
            // TUI is stateless per launch — it only needs live events. Replayed
            // events lack original timestamps (AgentEventType→TaskEvent uses
            // Utc::now()), so they'd appear as "new" and pollute a fresh TUI
            // session with stale messages. For targeted watches, replay is kept
            // so clients can catch up on a specific task's history.
            let replayed_count = {
                let s = task_store.lock().await;
                if task_id.is_empty() {
                    // Skip replay for "watch all" — TUI doesn't need history
                    0u64
                } else {
                    let events = s.read_events_from(0);
                    let mut count: u64 = 0;
                    for event in events.iter() {
                        let proto_event: TaskEvent = event.clone().into();
                        if proto_event.task_id != task_id {
                            continue;
                        }
                        yield Ok(proto_event);
                        count += 1;
                    }
                    count
                }
            };

            // Phase 2: listen for new events via broadcast.
            // The receiver was created before Phase 1, so there is no gap.
            // Dedup: the broadcast buffer holds events from the subscribe point,
            // so Phase 2 may re-deliver events we just replayed in Phase 1. Skip
            // the first `replayed_count` matching events (the replayed ones were
            // the oldest in the buffer at subscribe time).
            // (The original event_idx-based dedup was dead code — event_idx was
            // never set on events — so Phase 2 re-sent every replayed event,
            // causing duplicate dashboard updates.)
            let mut rx = event_rx;
            let mut skip_remaining = replayed_count;
            loop {
                match rx.recv().await {
                    Ok(proto_event) => {
                        if !task_id.is_empty() && proto_event.task_id != task_id {
                            continue;
                        }
                        // Skip replayed events that overlap with Phase 1.
                        if skip_remaining > 0 {
                            skip_remaining -= 1;
                            continue;
                        }
                        yield Ok(proto_event);
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!(
                            skipped = n,
                            "WatchTask broadcast receiver lagged, some events dropped"
                        );
                        // After a lag, the skip_remaining dedup counter is no
                        // longer accurate (the receiver skipped N events, whose
                        // task_ids are unknown). Stop deduping to avoid wrongly
                        // skipping new events — the sync_required event below
                        // prompts the client to re-sync, which is the correct
                        // recovery path.
                        skip_remaining = 0;
                        // Notify client that it missed events and should re-sync
                        let sync_event = TaskEvent {
                            timestamp: chrono::Utc::now().to_rfc3339(),
                            r#type: "sync_required".to_string(),
                            task_id: String::new(),
                            subtask_id: None,
                            data: HashMap::from([
                                ("reason".to_string(), "broadcast_lagged".to_string()),
                                ("skipped".to_string(), n.to_string()),
                            ]),
                        };
                        yield Ok(sync_event);
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        tracing::debug!("WatchTask broadcast channel closed, ending stream");
                        break;
                    }
                }
            }
        };

        Ok(Response::new(Box::pin(stream)))
    }

    async fn pause_task(
        &self,
        request: Request<PauseTaskRequest>,
    ) -> Result<Response<PauseTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
        let result = {
            let mut store = self.inner.task_store.lock().await;
            match store.pause_task(&task_id) {
                Ok(task) => {
                    // Record event + broadcast to WatchTask streams
                    let event = uc_engine::AgentEventType::TaskPaused {
                        task_id: task.id.clone(),
                    };
                    store.record_event(event.clone());
                    let proto_event: TaskEvent = event.into();
                    drop(store);
                    let _ = self.inner.event_tx.send(proto_event);
                    Ok(task)
                }
                Err(e) => Err(e),
            }
        };
        match result {
            Ok(task) => {
                // T6 #642 D6 — soft pause + grace hard stop: arm the timer.
                // A still-RUNNING attempt is failed via the graph plane
                // (`pause_grace_expired`) once the window lapses; resume
                // cancels the timer. Re-pause replaces it.
                let grace = std::time::Duration::from_secs(pause_grace_secs_from_env());
                #[cfg(feature = "messaging")]
                let pause_nats = self.inner.nats_client.clone();
                #[cfg(not(feature = "messaging"))]
                let pause_nats = ();
                spawn_pause_grace_timer(
                    task_id.clone(),
                    grace,
                    self.inner.task_store.clone(),
                    self.inner.pause_grace_timers.clone(),
                    pause_nats,
                );
                // Publish NATS event for Python side
                self.publish_task_status_event(&task_id, "task_paused")
                    .await;
                Ok(Response::new(PauseTaskResponse {
                    success: true,
                    task_id: task.id.0,
                    status: task_status_to_proto(&task.status).to_string(),
                    error: None,
                }))
            }
            Err(e) => Ok(Response::new(PauseTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }

    async fn resume_task(
        &self,
        request: Request<ResumeTaskRequest>,
    ) -> Result<Response<ResumeTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
        let result = {
            let mut store = self.inner.task_store.lock().await;
            match store.resume_task(&task_id) {
                Ok(task) => {
                    // Record event + broadcast to WatchTask streams
                    let event = uc_engine::AgentEventType::TaskResumed {
                        task_id: task.id.clone(),
                    };
                    store.record_event(event.clone());
                    let proto_event: TaskEvent = event.into();
                    drop(store);
                    let _ = self.inner.event_tx.send(proto_event);
                    Ok(task)
                }
                Err(e) => Err(e),
            }
        };
        match result {
            Ok(task) => {
                // T6 #642 D6 — resume disarms the pause-grace timer and
                // re-dispatches immediately: the paused task's dispatch gate
                // is open again, and every node that re-armed (grace expiry)
                // or flipped READY while paused (commit-time recompute is
                // transactional, so no extra graph recompute is needed here)
                // goes out to workers in the same breath.
                cancel_pause_grace_timer(&task_id, self.inner.pause_grace_timers.clone());
                #[cfg(feature = "messaging")]
                if let Some(client) = self.inner.nats_client.clone() {
                    dispatch_ready_subtasks(
                        &self.inner.task_store,
                        &self.inner.worker_registry,
                        &client,
                        &task_id,
                    )
                    .await;
                }
                // Best-effort: recover from the event log and log any drift
                // between the in-memory TaskStore and the reconstructed state.
                // ponytail: recover is advisory here — full state reconciliation
                // from TaskSnapshot is lossy (no depends_on/file_constraints),
                // so we surface drift via logs rather than rewriting TaskStore.
                // Upgrade path: reconstruct Task from snapshot when TaskStore
                // loses a task on restart (needs richer snapshot fields).
                if let Ok(snapshot) = self.inner.checkpoint_manager.recover(&task_id).await {
                    let drift = snapshot
                        .subtasks
                        .iter()
                        .filter(|s| {
                            !task.subtasks.iter().any(|t| {
                                t.id.0 == s.subtask_id
                                    && subtask_status_to_proto(&t.status)
                                        .eq_ignore_ascii_case(&s.status)
                            })
                        })
                        .count();
                    if drift > 0 {
                        tracing::warn!(
                            task_id = %task_id,
                            drift_count = drift,
                            recovered_subtasks = snapshot.subtasks.len(),
                            "resume: recovered subtask state drifted from TaskStore"
                        );
                    }
                }
                // Publish NATS event for Python side
                self.publish_task_status_event(&task_id, "task_resumed")
                    .await;
                Ok(Response::new(ResumeTaskResponse {
                    success: true,
                    task_id: task.id.0,
                    status: task_status_to_proto(&task.status).to_string(),
                    error: None,
                }))
            }
            Err(e) => Ok(Response::new(ResumeTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }

    async fn create_checkpoint(
        &self,
        request: Request<CreateCheckpointRequest>,
    ) -> Result<Response<CreateCheckpointResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
        match self
            .inner
            .checkpoint_manager
            .create_snapshot(&task_id)
            .await
        {
            Ok(snapshot_id) => Ok(Response::new(CreateCheckpointResponse {
                success: true,
                task_id,
                snapshot_id,
                error: None,
            })),
            Err(e) => Ok(Response::new(CreateCheckpointResponse {
                success: false,
                task_id,
                snapshot_id: String::new(),
                error: Some(e.to_string()),
            })),
        }
    }

    async fn recover_task(
        &self,
        request: Request<RecoverTaskRequest>,
    ) -> Result<Response<RecoverTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
        match self.inner.checkpoint_manager.recover(&task_id).await {
            Ok(snapshot) => Ok(Response::new(RecoverTaskResponse {
                success: true,
                task_id,
                snapshot: Some(task_snapshot_to_proto(&snapshot)),
                error: None,
            })),
            Err(e) => Ok(Response::new(RecoverTaskResponse {
                success: false,
                task_id,
                snapshot: None,
                error: Some(e.to_string()),
            })),
        }
    }

    async fn cancel_task(
        &self,
        request: Request<CancelTaskRequest>,
    ) -> Result<Response<CancelTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();

        // T7 #643 — granularity dispatch. `subtask_id` + `attempt_no` →
        // attempt-level cancel (cancel-attempt-keep-node); bare
        // `subtask_id` → node-level cancel (downstream closure goes
        // CANCELLED); neither → legacy task-level cancel.
        if let Some(node_id) = req.subtask_id.clone() {
            let response = if req.attempt_no.is_some() {
                self.cancel_task_attempt_granular(&task_id, &node_id).await
            } else {
                self.cancel_task_nodes_granular(&task_id, &[node_id]).await
            };
            return Ok(Response::new(response));
        }

        let result = {
            let mut store = self.inner.task_store.lock().await;
            match store.cancel_task(&task_id) {
                Ok(task) => {
                    // Record event + broadcast to WatchTask streams
                    let event = uc_engine::AgentEventType::TaskCancelled {
                        task_id: task.id.clone(),
                    };
                    store.record_event(event.clone());
                    let proto_event: TaskEvent = event.into();
                    drop(store);
                    let _ = self.inner.event_tx.send(proto_event);
                    Ok(task)
                }
                Err(e) => Err(e),
            }
        };
        match result {
            Ok(task) => {
                // Publish NATS event for Python side
                self.publish_task_status_event(&task_id, "task_cancelled")
                    .await;
                // T7 #643 — a task-level cancel must also stop the worker
                // processes already running for it: fail every RUNNING
                // attempt through the graph plane and emit
                // `attempt_cancelled` per swept attempt (the worker's kill
                // trigger). The graph nodes re-arm to READY, but the
                // cancelled task's dispatch gate is shut so nothing re-runs.
                let swept = {
                    let store = self.inner.task_store.lock().await;
                    match store.graph_shadow() {
                        Some(sink) => sink.fail_running_attempts(&task_id, "task_cancelled").await,
                        None => Vec::new(),
                    }
                };
                for s in &swept {
                    let mut data = serde_json::Map::new();
                    data.insert(
                        "attempt_id".to_string(),
                        serde_json::Value::String(s.attempt_id.clone()),
                    );
                    data.insert("rearmed".to_string(), serde_json::Value::Bool(s.rearmed));
                    data.insert(
                        "reason".to_string(),
                        serde_json::Value::String("task_cancelled".to_string()),
                    );
                    self.publish_task_control_event(
                        &task_id,
                        "attempt_cancelled",
                        Some(&s.node_id),
                        data,
                    )
                    .await;
                }
                Ok(Response::new(CancelTaskResponse {
                    success: true,
                    task_id: task.id.0,
                    status: task_status_to_proto(&task.status).to_string(),
                    error: None,
                }))
            }
            Err(e) => Ok(Response::new(CancelTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }

    async fn update_task(
        &self,
        request: Request<UpdateTaskRequest>,
    ) -> Result<Response<UpdateTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
        let status_str = req.status.clone();
        let description = req.description.clone();
        let project_id = req.project_id.clone();

        // Convert proto subtasks to Rust Subtask type
        let subtasks: Vec<uc_types::Subtask> = req
            .subtasks
            .into_iter()
            .map(|st| {
                let sub_status = proto_subtask_status_from_str(&st.status)
                    .unwrap_or(uc_types::SubtaskStatus::Pending);
                uc_types::Subtask {
                    id: uc_types::TaskId(st.id),
                    parent_id: uc_types::TaskId(task_id.clone()),
                    description: st.description,
                    status: sub_status,
                    assigned_worker: st.assigned_worker.map(uc_types::WorkerId),
                    depends_on: st.depends_on.into_iter().map(uc_types::TaskId).collect(),
                    file_constraints: st.file_constraints,
                    expected_output: st.expected_output,
                    result: None, // ponytail: SubtaskResult is complex struct; result tracked via SubtaskCompleted events
                    dispatch_mode: st.dispatch_mode.as_deref().map_or_else(
                        uc_types::DispatchMode::default,
                        |s| match s {
                            "Remote" => uc_types::DispatchMode::Remote,
                            _ => uc_types::DispatchMode::PreferRemote,
                        },
                    ),
                    effect_class: uc_types::EffectClass::default(),
                    dispatch_retry_count: st.dispatch_retry_count.unwrap_or(0),
                    retry_count: st.retry_count.unwrap_or(0),
                    required_capabilities: st.required_capabilities,
                    agent_config_json: None,
                    steps: st
                        .steps
                        .iter()
                        .map(|s| uc_types::WorkflowStep {
                            agent: s.agent.clone(),
                            prompt: s.prompt.clone(),
                            agent_config_json: s.agent_config_json.clone(),
                            abort_on_failure: s.abort_on_failure.unwrap_or(true),
                            retry_count: s.retry_count.unwrap_or(0),
                            retry_delay_ms: s.retry_delay_ms.unwrap_or(0),
                            condition: s.condition.clone(),
                            parallel_group: s.parallel_group.clone(),
                        })
                        .collect(),
                }
            })
            .collect();

        let result = {
            let mut store = self.inner.task_store.lock().await;
            match store.update_task(&task_id, &status_str, subtasks, &description, &project_id) {
                Ok((task, events)) => {
                    // Convert all events to proto and broadcast to WatchTask streams
                    let tx = &self.inner.event_tx;
                    for event in &events {
                        let proto_event: TaskEvent = event.clone().into();
                        let _ = tx.send(proto_event);
                    }
                    Ok(task)
                }
                Err(e) => Err(e),
            }
        };

        match result {
            Ok(task) => {
                // Dispatch ready subtasks to NATS workers after update.
                // This bridges the gap where upsertTask (from OMP) populates
                // subtasks but the server never triggers dispatch — the NATS
                // subscriber path already does this for Python-originated updates.
                self.publish_ready_subtasks(&task.id.0).await;

                Ok(Response::new(UpdateTaskResponse {
                    success: true,
                    task_id: task.id.0,
                    status: task_status_to_proto(&task.status).to_string(),
                    error: None,
                }))
            }
            Err(e) => Ok(Response::new(UpdateTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }

    /// T9 #651 / D9 #646 — merge-barrier grant issuance. The gateway is the
    /// single writer: the arbiter must hold a grant BEFORE merging. The grant
    /// is gated on graph quiescence and carries a deterministic
    /// `merge_idempotency_key` binding it to the exact SUCCEEDED set + output
    /// hashes, so a stale aggregation presents a key the gateway no longer
    /// knows.
    async fn issue_merge_grant(
        &self,
        request: Request<IssueMergeGrantRequest>,
    ) -> Result<Response<IssueMergeGrantResponse>, Status> {
        let req = request.into_inner();
        let graph_id = req.graph_id;
        if graph_id.trim().is_empty() {
            return Ok(Response::new(IssueMergeGrantResponse {
                granted: false,
                merge_idempotency_key: String::new(),
                idempotent_replay: false,
                error: Some("graph_id cannot be empty".to_string()),
            }));
        }
        let decision = {
            let store = self.inner.task_store.lock().await;
            match store.graph_shadow() {
                Some(sink) => sink.issue_merge_grant(&graph_id).await,
                None => uc_types::MergeGrantDecision {
                    granted: false,
                    merge_idempotency_key: String::new(),
                    idempotent_replay: false,
                    error: "graph plane not configured".to_string(),
                },
            }
        };
        Ok(Response::new(IssueMergeGrantResponse {
            granted: decision.granted,
            merge_idempotency_key: decision.merge_idempotency_key,
            idempotent_replay: decision.idempotent_replay,
            error: if decision.error.is_empty() {
                None
            } else {
                Some(decision.error)
            },
        }))
    }

    /// T9 #651 / D9 #646 — merge-barrier outcome report. Unknown/superseded
    /// key → `accepted=false` (the report is dropped loudly); consumed-key
    /// replay → `accepted=true, idempotent_replay=true` (no-op).
    async fn report_merge_outcome(
        &self,
        request: Request<ReportMergeOutcomeRequest>,
    ) -> Result<Response<ReportMergeOutcomeResponse>, Status> {
        let req = request.into_inner();
        let graph_id = req.graph_id;
        let key = req.merge_idempotency_key;
        if graph_id.trim().is_empty() {
            return Ok(Response::new(ReportMergeOutcomeResponse {
                accepted: false,
                idempotent_replay: false,
            }));
        }
        let outcome = uc_types::MergeOutcomeReport {
            status: req.status,
            merged_branches: req.merged_branches,
            conflict_branches: req.conflict_branches,
            push_status: req.push_status,
        };
        let decision = {
            let store = self.inner.task_store.lock().await;
            match store.graph_shadow() {
                Some(sink) => sink.report_merge_outcome(&graph_id, &key, &outcome).await,
                None => uc_types::MergeReportDecision {
                    accepted: false,
                    idempotent_replay: false,
                },
            }
        };
        Ok(Response::new(ReportMergeOutcomeResponse {
            accepted: decision.accepted,
            idempotent_replay: decision.idempotent_replay,
        }))
    }
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {}

#[cfg(test)]
mod tests {
    use super::*;
    use futures::stream;
    // TaskStoreBackend trait methods (get_task, etc.) are needed for backend
    // write-path persistence tests.
    use uc_engine::TaskStoreBackend;

    #[tokio::test]
    async fn nats_subscriber_input_reports_any_closed_subscription() {
        for expected_subject in [
            NATS_SUBJECT_TASK_UPDATE,
            NATS_SUBJECT_TASK_EVENT,
            NATS_SUBJECT_HEARTBEAT,
        ] {
            let mut snapshot_interval =
                tokio::time::interval(std::time::Duration::from_secs(3_600));
            // Tokio intervals tick immediately. Consume that first tick so the
            // closed stream is the only ready branch in the selector below.
            snapshot_interval.tick().await;

            let make_stream = |subject| -> std::pin::Pin<Box<dyn futures::Stream<Item = u8>>> {
                if subject == expected_subject {
                    Box::pin(stream::empty())
                } else {
                    Box::pin(stream::pending())
                }
            };
            let mut update_sub = make_stream(NATS_SUBJECT_TASK_UPDATE);
            let mut event_sub = make_stream(NATS_SUBJECT_TASK_EVENT);
            let mut heartbeat_sub = make_stream(NATS_SUBJECT_HEARTBEAT);

            match next_nats_subscriber_input(
                &mut snapshot_interval,
                &mut update_sub,
                &mut event_sub,
                &mut heartbeat_sub,
            )
            .await
            {
                NatsSubscriberInput::SubscriptionEnded(subject) => {
                    assert_eq!(subject, expected_subject);
                }
                _ => panic!("expected the closed NATS subscription to end the cycle"),
            }
        }
    }

    #[test]
    fn error_mapping_search() {
        let status = to_status(uc_types::EngineError::SearchError("test".into()));
        assert_eq!(status.code(), tonic::Code::Internal);
    }

    #[test]
    fn error_mapping_index_not_found() {
        let status = to_status(uc_types::EngineError::IndexError("repo-1".into()));
        assert_eq!(status.code(), tonic::Code::NotFound);
    }

    #[test]
    fn error_mapping_not_found() {
        let status = to_status(uc_types::EngineError::NotFound("resource xyz".into()));
        assert_eq!(status.code(), tonic::Code::NotFound);
        assert_eq!(status.message(), "resource xyz");
    }

    #[test]
    fn error_mapping_connection() {
        let status = to_status(uc_types::EngineError::ConnectionError("refused".into()));
        assert_eq!(status.code(), tonic::Code::Unavailable);
    }

    #[test]
    fn error_mapping_timeout() {
        let status = to_status(uc_types::EngineError::TimeoutError("30s".into()));
        assert_eq!(status.code(), tonic::Code::DeadlineExceeded);
    }

    #[test]
    fn error_mapping_rate_limited() {
        let status = to_status(uc_types::EngineError::RateLimited(5));
        assert_eq!(status.code(), tonic::Code::ResourceExhausted);
    }

    #[test]
    fn error_mapping_conflict() {
        let status = to_status(uc_types::EngineError::ConflictError {
            path: "src/main.rs".into(),
            details: "overlap".into(),
        });
        assert_eq!(status.code(), tonic::Code::Aborted);
    }

    #[test]
    fn task_store_submit_and_get() {
        let mut store = TaskStore::new();
        let task = store.submit_task(
            "1. Analyze code\n2. Fix bug\n3. Write tests".to_string(),
            "project-1".to_string(),
        );

        // submit_task creates InProgress task with one subtask
        // (production uses submit_task_pending — decomposition happens in Python)
        assert_eq!(task.subtasks.len(), 1);
        assert_eq!(task.status, uc_types::TaskStatus::InProgress);

        // Get the task back
        let retrieved = store.get_task(&task.id.0).unwrap();
        assert_eq!(
            retrieved.description,
            "1. Analyze code\n2. Fix bug\n3. Write tests"
        );
    }

    #[test]
    fn task_store_submit_pending() {
        let mut store = TaskStore::new();
        let (task, _) =
            store.submit_task_pending("Fix the login bug".to_string(), "project-1".to_string());

        assert_eq!(task.status, uc_types::TaskStatus::Planning);
        assert!(task.subtasks.is_empty());

        // TaskCreated event should be recorded
        assert!(store.event_count() >= 1);

        // Get the task back
        let retrieved = store.get_task(&task.id.0).unwrap();
        assert_eq!(retrieved.status, uc_types::TaskStatus::Planning);
    }

    #[test]
    fn task_store_pause_and_resume() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, uc_types::TaskStatus::Paused);

        let resumed = store.resume_task(&task_id).unwrap();
        assert_eq!(resumed.status, uc_types::TaskStatus::InProgress);
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
        assert_eq!(paused.status, uc_types::TaskStatus::Paused);

        // Pause again (invalid: Paused -> Paused)
        let result = store.pause_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_resume_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Resume without pausing first (invalid: InProgress -> InProgress)
        let result = store.resume_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_cancel_valid() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let cancelled = store.cancel_task(&task_id).unwrap();
        assert_eq!(cancelled.status, uc_types::TaskStatus::Failed);
        // Subtask should also be marked Failed
        assert_eq!(cancelled.subtasks.len(), 1);
        assert_eq!(
            cancelled.subtasks[0].status,
            uc_types::SubtaskStatus::Failed
        );
    }

    #[test]
    fn task_store_cancel_paused() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Pause first, then cancel
        store.pause_task(&task_id).unwrap();
        let cancelled = store.cancel_task(&task_id).unwrap();
        assert_eq!(cancelled.status, uc_types::TaskStatus::Failed);
    }

    #[test]
    fn task_store_cancel_completed() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Manually set task to Completed
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.status = uc_types::TaskStatus::Completed;
        }

        // Cannot cancel a completed task
        let result = store.cancel_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_cancel_nonexistent() {
        let mut store = TaskStore::new();
        let result = store.cancel_task("nonexistent");
        assert!(result.is_err());
    }

    #[test]
    fn task_store_list_tasks() {
        let mut store = TaskStore::new();
        store.submit_task("Task 1".to_string(), "p1".to_string());
        store.submit_task("Task 2".to_string(), "p1".to_string());

        let tasks = store.list_tasks();
        assert_eq!(tasks.len(), 2);
    }

    #[test]
    fn task_store_events() {
        let mut store = TaskStore::new();
        store.submit_task("Test task".to_string(), "p1".to_string());

        // Should have TaskCreated + SubtaskAssigned events
        assert!(store.event_count() >= 2);

        // Read from offset 0
        let events = store.read_events_from(0);
        assert!(!events.is_empty());

        // Read from beyond end
        let events = store.read_events_from(100);
        assert!(events.is_empty());
    }

    // Rust-side task decomposition was removed in T5 #641 — all
    // decomposition goes through the Python Orchestrator via NATS/bridge.

    #[test]
    fn task_status_to_proto_conversion() {
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Created),
            "Created"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::InProgress),
            "InProgress"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Paused),
            "Paused"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Completed),
            "Completed"
        );
    }

    #[test]
    fn subtask_status_to_proto_conversion() {
        use crate::conversions::subtask_status_to_proto;
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::Pending),
            "Pending"
        );
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::InProgress),
            "InProgress"
        );
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::Conflicted),
            "Conflicted"
        );
    }

    // ── NATS protocol tests ──────────────────────────────────

    #[test]
    fn nats_task_submit_serialization() {
        let msg = NatsTaskSubmit {
            task_id: "abc-123".to_string(),
            description: "Fix the login bug".to_string(),
            project_id: "proj-1".to_string(),
            scheduled: None,
            verify_command: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        // `scheduled: None` must be absent from serialized JSON (skip_serializing_if).
        assert!(
            !json.contains("scheduled"),
            "scheduled=None must not appear in JSON: {}",
            json
        );
        let parsed: NatsTaskSubmit = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.task_id, "abc-123");
        assert_eq!(parsed.description, "Fix the login bug");
        assert_eq!(parsed.project_id, "proj-1");
        assert_eq!(parsed.scheduled, None);
    }

    #[test]
    fn nats_task_update_serialization() {
        let msg = NatsTaskUpdate {
            message_id: Some("abc-123:update::1700000000".to_string()),
            task_id: "abc-123".to_string(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsTaskUpdate = serde_json::from_str(&json).unwrap();
        assert_eq!(
            parsed.message_id,
            Some("abc-123:update::1700000000".to_string())
        );
        assert_eq!(parsed.task_id, "abc-123");
        assert_eq!(parsed.status, "InProgress");
        assert_eq!(parsed.subtasks.len(), 1);
        assert_eq!(parsed.subtasks[0].subtask_id, "st-1");
        assert_eq!(
            parsed.subtasks[0].assigned_worker,
            Some("worker-1".to_string())
        );
    }

    #[test]
    fn nats_task_event_serialization() {
        let mut data = serde_json::Map::new();
        data.insert(
            "tool_name".to_string(),
            serde_json::Value::String("grep".to_string()),
        );
        data.insert(
            "tool_input".to_string(),
            serde_json::Value::String("pattern".to_string()),
        );

        let msg = NatsTaskEvent {
            v: default_event_version(),
            message_id: Some("abc-123:tool_call:st-1:1700000000".to_string()),
            r#type: "tool_call".to_string(),
            task_id: "abc-123".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsTaskEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(
            parsed.message_id,
            Some("abc-123:tool_call:st-1:1700000000".to_string())
        );
        assert_eq!(parsed.r#type, "tool_call");
        assert_eq!(parsed.task_id, "abc-123");
        assert_eq!(parsed.subtask_id, Some("st-1".to_string()));
    }

    #[test]
    fn nats_heartbeat_serialization() {
        let msg = NatsHeartbeat {
            consumer_id: "consumer-1".to_string(),
            timestamp: "2026-06-16T12:00:00Z".to_string(),
            stale_dispatch_dropped: Some(2),
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsHeartbeat = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.consumer_id, "consumer-1");
        assert_eq!(parsed.timestamp, "2026-06-16T12:00:00Z");
        assert_eq!(parsed.stale_dispatch_dropped, Some(2));
        // Legacy heartbeat without the counter field parses with None.
        let legacy: NatsHeartbeat =
            serde_json::from_str(r#"{"consumer_id":"c","timestamp":"t"}"#).unwrap();
        assert_eq!(legacy.stale_dispatch_dropped, None);
    }

    #[test]
    fn task_status_from_str_roundtrip() {
        let statuses = [
            "Created",
            "Planning",
            "InProgress",
            "Completed",
            "Failed",
            "Paused",
        ];
        for s in &statuses {
            let status = task_status_from_str(s).unwrap();
            assert_eq!(task_status_to_proto(&status), *s);
        }
    }

    #[test]
    fn task_status_from_str_unknown() {
        assert!(task_status_from_str("Unknown").is_none());
        assert!(task_status_from_str("").is_none());
    }

    #[test]
    fn subtask_status_from_str_roundtrip() {
        use crate::conversions::subtask_status_to_proto;
        let statuses = [
            "Pending",
            "Assigned",
            "InProgress",
            "Completed",
            "Failed",
            "Conflicted",
        ];
        for s in &statuses {
            let status = subtask_status_from_str(s).unwrap();
            assert_eq!(subtask_status_to_proto(&status), *s);
        }
    }

    #[test]
    fn subtask_status_from_str_unknown() {
        assert!(subtask_status_from_str("Unknown").is_none());
    }

    #[test]
    fn task_store_apply_update_existing_task() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Apply an update that changes task status and adds a new subtask
        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-new-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(updated.status, uc_types::TaskStatus::InProgress);
        assert_eq!(updated.subtasks.len(), 2); // original 1 + new 1
        assert_eq!(updated.subtasks[1].id.0, "st-new-1");
        assert_eq!(
            updated.subtasks[1].status,
            uc_types::SubtaskStatus::Assigned
        );
    }

    #[test]
    fn task_store_apply_update_unknown_task() {
        let mut store = TaskStore::new();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: "nonexistent".to_string(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![],
            result: None,
        };

        // Should not panic, just log a warning
        store.apply_update(&update);
        assert!(store.get_task("nonexistent").is_none());
    }

    #[test]
    fn test_task_store_rehydrates_from_complete_nats_snapshot() {
        let raw = serde_json::json!({
            "v": 1,
            "tasks": [{
                "message_id": "t-recover:update:abc",
                "task_id": "t-recover",
                "description": "restore the task",
                "project_id": "project-a",
                "status": "InProgress",
                "partial": false,
                "subtasks": [{
                    "subtask_id": "t-recover-s0",
                    "status": "Assigned",
                    "description": "first step",
                    "depends_on": [],
                    "assigned_worker": "worker-a"
                }]
            }]
        });
        let response: NatsTaskSnapshotResponse = serde_json::from_value(raw).unwrap();
        let mut store = TaskStore::new();

        assert_eq!(apply_task_snapshot_response(&mut store, response), 1);

        let task = store
            .get_task("t-recover")
            .expect("snapshot rehydrates task");
        assert_eq!(task.description, "restore the task");
        assert_eq!(task.project_id, "project-a");
        assert_eq!(task.status, uc_types::TaskStatus::InProgress);
        assert_eq!(task.subtasks.len(), 1);
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Assigned);
        assert_eq!(store.event_count(), 1); // TaskCreated for the rehydrated task
    }

    #[test]
    fn test_task_store_does_not_rehydrate_from_partial_nats_update() {
        let mut store = TaskStore::new();
        let update = NatsTaskUpdate {
            message_id: None,
            task_id: "t-partial-unknown".to_string(),
            status: "InProgress".to_string(),
            partial: true,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-only".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-a".to_string()),
                description: Some("only one result".to_string()),
                depends_on: None,
                result: Some("done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update_with_metadata(&update, Some("should not create"), Some("project-a"));

        assert!(store.get_task("t-partial-unknown").is_none());
    }

    #[test]
    fn test_task_store_requires_project_id_for_rehydration() {
        let raw = serde_json::json!({
            "task_id": "t-missing-project",
            "status": "InProgress",
            "partial": false,
            "description": "restore the task",
            "subtasks": []
        });
        let envelope: NatsTaskUpdateEnvelope = serde_json::from_value(raw).unwrap();
        let mut store = TaskStore::new();

        store.apply_update_with_metadata(
            &envelope.update,
            envelope.description.as_deref(),
            envelope.project_id.as_deref(),
        );

        assert!(store.get_task("t-missing-project").is_none());
    }

    #[test]
    fn task_store_apply_update_unknown_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "BogusStatus".to_string(),
            partial: false,
            subtasks: vec![],
            result: None,
        };

        store.apply_update(&update);

        // Status should remain unchanged (unknown status is ignored)
        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(updated.status, uc_types::TaskStatus::InProgress);
    }

    #[test]
    fn task_store_unknown_status_does_not_trigger_terminal_derivation() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let Some(task) = store.tasks.get_mut(&task_id) else {
            panic!("task inserted by submit_task_pending");
        };
        task.subtasks.push(uc_types::Subtask {
            id: uc_types::TaskId("st-terminal".to_string()),
            parent_id: task.id.clone(),
            description: "already terminal".to_string(),
            status: uc_types::SubtaskStatus::Completed,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        });

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "BogusStatus".to_string(),
            partial: false,
            subtasks: Vec::new(),
            result: None,
        });

        assert_eq!(
            store.get_task(&task_id).map(|task| task.status.clone()),
            Some(uc_types::TaskStatus::Planning)
        );
    }

    #[test]
    fn task_store_apply_update_subtask_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let subtask_id = task.subtasks[0].id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "Completed".to_string(),
                assigned_worker: None,
                description: None,
                depends_on: None,
                result: Some("Done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(
            updated.subtasks[0].status,
            uc_types::SubtaskStatus::Completed
        );
    }

    #[test]
    fn task_store_apply_update_derives_terminal_status_from_all_subtasks() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // The Python Orchestrator publishes the complete decomposition before
        // workers report individual results. A single completed subtask must
        // not complete a task while another subtask is still pending.
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![
                NatsSubtaskUpdate {
                    subtask_id: "st-a".to_string(),
                    status: "Pending".to_string(),
                    assigned_worker: None,
                    description: Some("first".to_string()),
                    depends_on: None,
                    result: None,
                    attempt_id: None,
                    usage: None,
                    steps: None,

                    review: None,
                },
                NatsSubtaskUpdate {
                    subtask_id: "st-b".to_string(),
                    status: "Pending".to_string(),
                    assigned_worker: None,
                    description: Some("second".to_string()),
                    depends_on: None,
                    result: None,
                    attempt_id: None,
                    usage: None,
                    steps: None,

                    review: None,
                },
            ],
            result: None,
        });

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-a".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: Some("first done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        assert_eq!(
            store.get_task(&task_id).map(|task| task.status.clone()),
            Some(uc_types::TaskStatus::InProgress)
        );

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-b".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-2".to_string()),
                description: None,
                depends_on: None,
                result: Some("second done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        assert_eq!(
            store.get_task(&task_id).map(|task| task.status.clone()),
            Some(uc_types::TaskStatus::Completed)
        );
    }

    #[test]
    fn task_store_apply_update_derives_failed_status_from_terminal_subtask() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-fail".to_string(),
                status: "Failed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: Some("fails".to_string()),
                depends_on: None,
                result: Some("boom".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });

        assert_eq!(
            store.get_task(&task_id).map(|task| task.status.clone()),
            Some(uc_types::TaskStatus::Failed)
        );
    }

    #[test]
    fn task_store_apply_update_partial_result_does_not_derive_terminal_status() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: true,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-partial".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: Some("partial result".to_string()),
                depends_on: None,
                result: Some("done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });

        assert_eq!(
            store.get_task(&task_id).map(|task| task.status.clone()),
            Some(uc_types::TaskStatus::InProgress)
        );
    }

    // ── T4 #640: late-result fencing ─────────────────────────────

    /// A worker-sourced (partial) result stamped with an attempt OLDER than
    /// the subtask's current attempt is rejected: status unchanged, counter
    /// bumped, a user-visible event recorded.
    #[test]
    fn task_store_fences_late_result_from_older_attempt() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Move the subtask to Assigned and re-dispatch once (attempt 1).
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-late".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        store.increment_dispatch_retry(&task_id, "st-late");

        let events_before = store.event_count();
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: true,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-late".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: Some("stale attempt 0 result".to_string()),
                attempt_id: Some(0),
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });

        let st = store
            .get_task(&task_id)
            .unwrap()
            .subtasks
            .iter()
            .find(|st| st.id.0 == "st-late")
            .unwrap();
        assert_eq!(st.status, uc_types::SubtaskStatus::Assigned);
        assert!(st.result.is_none(), "stale result must not overwrite");
        assert_eq!(store.stale_dispatch_dropped(), 1);
        assert_eq!(store.event_count(), events_before + 1);
    }

    /// The CURRENT attempt's result passes the fence and lands normally.
    #[test]
    fn task_store_accepts_result_from_current_attempt() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-cur".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        store.increment_dispatch_retry(&task_id, "st-cur");

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: true,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-cur".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: Some("current attempt result".to_string()),
                attempt_id: Some(1),
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });

        let st = store
            .get_task(&task_id)
            .unwrap()
            .subtasks
            .iter()
            .find(|st| st.id.0 == "st-cur")
            .unwrap();
        assert_eq!(st.status, uc_types::SubtaskStatus::Completed);
        assert_eq!(store.stale_dispatch_dropped(), 0);
    }

    /// Unstamped (legacy publisher) updates are never fenced, and complete
    /// snapshots (partial=false) bypass the fence entirely — the orchestrator
    /// does not track the gateway's attempt counter.
    #[test]
    fn task_store_unstamped_and_full_updates_pass_the_fence() {
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-legacy".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        store.increment_dispatch_retry(&task_id, "st-legacy");

        // Partial but UNSTAMPED (legacy worker): passes.
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: true,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-legacy".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: Some("legacy result".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        let st = store
            .get_task(&task_id)
            .unwrap()
            .subtasks
            .iter()
            .find(|st| st.id.0 == "st-legacy")
            .unwrap();
        assert_eq!(st.status, uc_types::SubtaskStatus::Completed);
        assert_eq!(store.stale_dispatch_dropped(), 0);

        // Complete snapshot stamped with an old attempt: NOT fenced.
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-legacy".to_string(),
                status: "Completed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: Some("snapshot result".to_string()),
                attempt_id: Some(0),
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        let st = store
            .get_task(&task_id)
            .unwrap()
            .subtasks
            .iter()
            .find(|st| st.id.0 == "st-legacy")
            .unwrap();
        assert_eq!(st.status, uc_types::SubtaskStatus::Completed);
        assert_eq!(store.stale_dispatch_dropped(), 0);
    }

    /// Heartbeat-reported stale-dispatch counters keep the monotonic max per
    /// worker (T4 #640 / D7).
    #[test]
    fn task_store_worker_stale_dispatch_counter_is_monotonic() {
        let mut store = TaskStore::new();
        store.update_worker_heartbeat("w1", Some(3));
        store.update_worker_heartbeat("w1", Some(1)); // older report — ignored
        store.update_worker_heartbeat("w1", None); // legacy heartbeat — ignored
        assert_eq!(store.worker_stale_dispatch_dropped().get("w1"), Some(&3));
        assert!(store.worker_stale_dispatch_dropped().get("w2").is_none());
        store.update_worker_heartbeat("w2", Some(7));
        assert_eq!(store.worker_stale_dispatch_dropped().get("w2"), Some(&7));
        assert_eq!(store.stale_dispatch_dropped(), 0);
    }

    /// Worker-published `stale_dispatch_dropped` events map to a task-level
    /// TaskUpdated so the drop is user-visible in the event feed (T4/D7).
    //
    // T4 added this test without the gate its siblings carry: the
    // `nats_event_to_agent_event` helper is `messaging`-gated while
    // `NatsTaskEvent` is always compiled, so an un-gated caller breaks the lib
    // test build whenever `messaging` is off — and uc-grpc is `default = []`,
    // which is exactly what CI's `test-default` / `test-no-storage` jobs build.
    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_stale_dispatch_dropped() {
        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "stale_dispatch_dropped".to_string(),
            task_id: "t-stale".to_string(),
            subtask_id: Some("st-1".to_string()),
            data: serde_json::json!({
                "reason": "missing_execution_envelope",
                "stale_dispatch_dropped": 2,
            })
            .as_object()
            .cloned()
            .unwrap(),
        };
        let mapped = nats_event_to_agent_event(&event).expect("must map");
        match mapped {
            uc_engine::AgentEventType::TaskUpdated { task_id, status } => {
                assert_eq!(task_id.0, "t-stale");
                assert_eq!(status, "stale_dispatch_dropped");
            }
            other => panic!("expected TaskUpdated, got {:?}", other),
        }
    }

    #[test]
    fn task_store_record_event() {
        let mut store = TaskStore::new();
        let initial_count = store.event_count();

        store.record_event(uc_engine::AgentEventType::TaskCreated {
            task_id: uc_types::TaskId::new(),
            description: "Extra event".to_string(),
        });

        assert_eq!(store.event_count(), initial_count + 1);
    }

    /// PR1: CheckpointManager attached to TaskStore's EventStore recovers
    /// state from events recorded under TaskStore's `task.{id}` subject.
    /// Proves the subject-prefix alignment (`task.`) is correct — without it,
    /// recover() would read `agent.events.{id}` and find nothing.
    #[tokio::test]
    async fn checkpoint_manager_recovers_taskstore_events() {
        use std::sync::Arc;

        let event_store: Arc<dyn uc_engine::EventStore> =
            Arc::new(uc_engine::InMemoryEventStore::new());
        let store = TaskStore::with_event_store(event_store.clone());

        let task_id = "ck-task-1";
        let subtask_id = uc_types::TaskId::new();
        let worker_id = uc_types::WorkerId::new();
        let subject = format!("task.{}", task_id);

        // Simulate production event recording (TaskStore.record_event_with_subject
        // appends under `task.{id}` via a spawned task; here we append directly
        // to avoid the fire-and-forget race in a unit test).
        event_store
            .append(
                &subject,
                &uc_engine::AgentEventType::TaskCreated {
                    task_id: uc_types::TaskId(task_id.to_string()),
                    description: "Checkpoint test".to_string(),
                },
            )
            .await
            .unwrap();
        event_store
            .append(
                &subject,
                &uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: uc_types::TaskId(task_id.to_string()),
                    subtask_id: subtask_id.clone(),
                    worker_id: worker_id.clone(),
                },
            )
            .await
            .unwrap();
        event_store
            .append(
                &subject,
                &uc_engine::AgentEventType::SubtaskCompleted {
                    task_id: uc_types::TaskId(task_id.to_string()),
                    subtask_id: subtask_id.clone(),
                    summary: "Done".to_string(),
                    success: true,
                    modified_files: Vec::new(),
                    output: String::new(),
                    simulated: false,
                },
            )
            .await
            .unwrap();

        // Build CheckpointManager the same way GrpcServer does.
        let config = uc_engine::CheckpointConfig {
            subject_prefix: "task.".to_string(),
            ..Default::default()
        };
        let ckpt = uc_engine::CheckpointManager::new(event_store.clone(), config);

        // Recover from scratch (no snapshot yet) — replays all 3 events.
        let state = ckpt.recover(task_id).await.unwrap();
        assert_eq!(state.task_id, task_id);
        assert_eq!(state.subtasks.len(), 1);
        assert_eq!(state.subtasks[0].status, "completed");
        assert_eq!(state.subtasks[0].result_summary.as_deref(), Some("Done"));

        // Create a snapshot, record one more event, recover → post-snapshot
        // event must be replayed on top of the snapshot.
        let snapshot_id = ckpt.create_snapshot(task_id).await.unwrap();
        assert!(!snapshot_id.is_empty());

        event_store
            .append(
                &subject,
                &uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: uc_types::TaskId(task_id.to_string()),
                    subtask_id: uc_types::TaskId::new(),
                    worker_id: uc_types::WorkerId::new(),
                },
            )
            .await
            .unwrap();

        let state2 = ckpt.recover(task_id).await.unwrap();
        assert_eq!(
            state2.subtasks.len(),
            2,
            "post-snapshot event should be replayed"
        );

        // Touch store so with_event_store path is exercised.
        assert_eq!(store.event_count(), 0);
    }

    #[test]
    fn task_store_heartbeat_tracking() {
        let mut store = TaskStore::new();

        // No heartbeat initially
        assert!(store.last_heartbeat().is_none());

        // Update heartbeat
        store.update_last_heartbeat();
        assert!(store.last_heartbeat().is_some());
    }

    #[test]
    fn task_store_mark_stale_tasks_no_heartbeat() {
        let mut store = TaskStore::new();
        store.submit_task("Test task".to_string(), "p1".to_string());

        // No heartbeat received — should not mark tasks as failed
        let failed = store.mark_stale_tasks_failed(std::time::Duration::from_secs(1));
        assert!(failed.is_empty());
    }

    #[test]
    fn task_store_mark_stale_tasks_with_heartbeat() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Record heartbeat
        store.update_last_heartbeat();

        // With a very long timeout, should not mark as failed
        let failed = store.mark_stale_tasks_failed(std::time::Duration::from_secs(9999));
        assert!(failed.is_empty());

        // With zero timeout (immediately stale), should mark as failed
        // Note: this test depends on the heartbeat timestamp being in the past
        // by even a tiny amount, which is always true since we recorded it
        // before calling mark_stale_tasks_failed.
        let failed = store.mark_stale_tasks_failed(std::time::Duration::ZERO);
        assert_eq!(failed.len(), 1);
        assert_eq!(failed[0], task_id);

        // Task should now be Failed
        let task = store.get_task(&failed[0]).unwrap();
        assert_eq!(task.status, uc_types::TaskStatus::Failed);
    }

    #[test]
    fn task_store_mark_stale_skips_completed_tasks() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Manually set task to Completed
        {
            let task = store.tasks.get_mut(&task_id).unwrap();
            task.status = uc_types::TaskStatus::Completed;
        }

        store.update_last_heartbeat();

        // Even with zero timeout, Completed tasks should not be marked Failed
        let failed = store.mark_stale_tasks_failed(std::time::Duration::ZERO);
        assert!(failed.is_empty());
    }

    #[test]
    fn nats_subjects_constants() {
        assert_eq!(NATS_SUBJECT_TASK_SUBMIT, "uc.task.submit");
        assert_eq!(NATS_SUBJECT_TASK_UPDATE, "uc.task.update");
        assert_eq!(
            NATS_SUBJECT_TASK_SNAPSHOT_REQUEST,
            "uc.task.snapshot.request"
        );
        assert_eq!(NATS_SUBJECT_TASK_EVENT, "uc.task.event");
        assert_eq!(NATS_SUBJECT_HEARTBEAT, "uc.heartbeat");
        assert_eq!(NATS_SUBJECT_SUBTASK_EXECUTE, "uc.subtask.execute");
    }

    #[test]
    fn task_store_get_ready_subtasks_no_deps() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        // Set task to InProgress so get_ready_subtasks considers it
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.status = uc_types::TaskStatus::InProgress;
        }
        let ready = store.get_ready_subtasks(&task_id);
        // Default submit creates 1 subtask with no deps — should be ready
        assert_eq!(ready.len(), 1);
        assert_eq!(ready[0].status, uc_types::SubtaskStatus::Pending);
    }

    #[test]
    fn task_store_get_ready_subtasks_with_unmet_deps() {
        let mut store = TaskStore::new();
        // Create a task and add a subtask with a dependency
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        // Add a second subtask that depends on the first
        let first_id = task.subtasks[0].id.0.clone();
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.status = uc_types::TaskStatus::InProgress;
            t.subtasks.push(uc_types::Subtask {
                id: uc_types::TaskId::new(),
                parent_id: task.id.clone(),
                description: "Dependent subtask".to_string(),
                status: uc_types::SubtaskStatus::Pending,
                assigned_worker: None,
                depends_on: vec![uc_types::TaskId(first_id.clone())],
                file_constraints: Vec::new(),
                expected_output: String::new(),
                result: None,
                dispatch_mode: uc_types::DispatchMode::default(),
                effect_class: uc_types::EffectClass::default(),
                dispatch_retry_count: 0,
                retry_count: 0,
                required_capabilities: Vec::new(),
                agent_config_json: None,
                steps: Vec::new(),
            });
        }
        // Only the first subtask (no deps) should be ready
        let ready = store.get_ready_subtasks(&task_id);
        assert_eq!(ready.len(), 1);
        assert_eq!(ready[0].id.0, first_id);
    }

    #[test]
    fn task_store_get_ready_subtasks_deps_met() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let first_id = task.subtasks[0].id.0.clone();
        // Add dependent subtask
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.status = uc_types::TaskStatus::InProgress;
            t.subtasks.push(uc_types::Subtask {
                id: uc_types::TaskId::new(),
                parent_id: task.id.clone(),
                description: "Dependent".to_string(),
                status: uc_types::SubtaskStatus::Pending,
                assigned_worker: None,
                depends_on: vec![uc_types::TaskId(first_id.clone())],
                file_constraints: Vec::new(),
                expected_output: String::new(),
                result: None,
                dispatch_mode: uc_types::DispatchMode::default(),
                effect_class: uc_types::EffectClass::default(),
                dispatch_retry_count: 0,
                retry_count: 0,
                required_capabilities: Vec::new(),
                agent_config_json: None,
                steps: Vec::new(),
            });
        }
        // Complete the first subtask
        store.update_subtask_status(&task_id, &first_id, uc_types::SubtaskStatus::Completed);
        // Now both should NOT be ready — first is Completed, second's deps are met
        let ready = store.get_ready_subtasks(&task_id);
        assert_eq!(ready.len(), 1);
        assert_ne!(ready[0].id.0, first_id); // The dependent one, not the completed one
    }

    // ── Worker heartbeat failover tests ──────────────────────

    #[test]
    fn worker_heartbeat_update_and_stale_detection() {
        let mut store = TaskStore::new();
        assert!(store
            .mark_stale_workers(std::time::Duration::from_secs(1))
            .is_empty());

        store.update_worker_heartbeat("worker-1", None);
        store.update_worker_heartbeat("worker-2", None);

        // With long timeout, none stale
        let stale = store.mark_stale_workers(std::time::Duration::from_secs(9999));
        assert!(stale.is_empty());

        // Workers were NOT removed (they weren't stale), so we can still detect them.
        // Manually backdate heartbeat to simulate aging.
        {
            let old_ts = chrono::Utc::now() - chrono::Duration::seconds(60);
            store
                .worker_heartbeats
                .insert("worker-1".to_string(), old_ts);
            store
                .worker_heartbeats
                .insert("worker-2".to_string(), old_ts);
        }

        // Now with 30s timeout, both are stale
        let stale = store.mark_stale_workers(std::time::Duration::from_secs(30));
        assert_eq!(stale.len(), 2);
        assert!(stale.contains(&"worker-1".to_string()));
        assert!(stale.contains(&"worker-2".to_string()));
    }

    // ── T6 (#642) sweep bridge ──────────────────────────────────────────
    //
    // The legacy reassign reapers are gone; the graph-plane sweep owns the
    // staleness verdict and `revert_swept_subtask` only mirrors the outcome.

    #[test]
    fn sweep_bridge_reverts_in_progress_to_pending() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let subtask_id = task.subtasks[0].id.0.clone();

        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            t.subtasks[0].assigned_worker = Some(uc_types::WorkerId("worker-1".to_string()));
        }

        assert!(store.revert_swept_subtask(&task_id, &subtask_id, true));

        // Verify subtask is back to Pending with no assigned worker
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Pending);
        assert!(task.subtasks[0].assigned_worker.is_none());
    }

    #[test]
    fn sweep_bridge_marks_failed_when_budget_exhausted() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::Assigned;
        }

        assert!(store.revert_swept_subtask(&task_id, &st_id, false));
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Failed);
    }

    #[test]
    fn sweep_bridge_never_touches_committed_nodes() {
        // A commit landing between the sweep and the bridge wins: the
        // bridged revert only moves InProgress/Assigned rows — committed
        // winners are immutable on the legacy side too.
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::Completed;
        }

        assert!(!store.revert_swept_subtask(&task_id, &st_id, true));
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Completed);
    }

    #[test]
    fn sweep_bridge_ignores_unknown_task_or_subtask() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        assert!(!store.revert_swept_subtask("no-such-task", &st_id, true));
        assert!(!store.revert_swept_subtask(&task_id, "no-such-subtask", true));
    }

    #[test]
    fn cancel_task_clears_assigned_subtask_tracking() {
        // Regression: cancel_task set subtask status directly (Failed) without
        // going through update_subtask_status, so assigned_subtask_times entries
        // for Assigned subtasks leaked. Over many cancels the map grew unbounded.
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();

        // Mark Assigned (records tracking entry), then cancel the task.
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        assert!(store.assigned_subtask_times.contains_key(&st_id));

        store.cancel_task(&task_id).unwrap();

        // Subtask is now Failed; tracking entry must be gone (not leaked).
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Failed);
        assert!(
            !store.assigned_subtask_times.contains_key(&st_id),
            "assigned_subtask_times entry leaked after cancel_task"
        );
    }

    #[test]
    fn stale_worker_must_be_deregistered_from_registry() {
        // Regression: mark_stale_workers only removed workers from the TaskStore
        // heartbeat map, never from the WorkerRegistry. A worker that went silent
        // stayed in the registry forever (listWorkers reported it as
        // is_available=false indefinitely), and the heartbeat monitor logged a
        // misleading "Reassigned subtasks" WARN every cycle even when nothing
        // was reassigned. The monitor now deregisters stale workers.
        let mut store = TaskStore::new();
        let mut registry = WorkerRegistry::new();

        // Worker registers and heartbeats.
        registry
            .register(
                "worker-stale".to_string(),
                vec!["rust".to_string()],
                2,
                String::new(),
                String::new(),
            )
            .unwrap();
        store.update_worker_heartbeat("worker-stale", None);
        assert!(registry.workers().contains_key("worker-stale"));

        // Backdate heartbeat so the worker is stale.
        let old_ts = chrono::Utc::now() - chrono::Duration::seconds(60);
        store
            .worker_heartbeats
            .insert("worker-stale".to_string(), old_ts);

        let stale = store.mark_stale_workers(std::time::Duration::from_secs(30));
        assert_eq!(stale, vec!["worker-stale".to_string()]);

        // Monitor now deregisters each stale worker from the registry.
        for wid in &stale {
            let _ = registry.deregister(wid);
        }
        assert!(!registry.workers().contains_key("worker-stale"));
    }

    // ── NATS event conversion tests ──────────────────────────

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_subtask_assigned() {
        let mut data = serde_json::Map::new();
        data.insert(
            "worker_id".to_string(),
            serde_json::Value::String("w-1".to_string()),
        );

        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "subtask_assigned".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };

        let result = nats_event_to_agent_event(&event);
        assert!(result.is_some());
        match result.unwrap() {
            uc_engine::AgentEventType::SubtaskAssigned {
                task_id,
                subtask_id,
                worker_id,
            } => {
                assert_eq!(task_id.0, "t-1");
                assert_eq!(subtask_id.0, "st-1");
                assert_eq!(worker_id.0, "w-1");
            }
            _ => panic!("Expected SubtaskAssigned"),
        }
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_unknown_type() {
        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "unknown_type".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: None,
            data: serde_json::Map::new(),
        };

        let result = nats_event_to_agent_event(&event);
        assert!(result.is_none());
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_tool_call() {
        let mut data = serde_json::Map::new();
        data.insert(
            "tool_name".to_string(),
            serde_json::Value::String("grep".to_string()),
        );
        data.insert(
            "tool_input".to_string(),
            serde_json::Value::String("pattern".to_string()),
        );

        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "tool_call".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };

        let result = nats_event_to_agent_event(&event);
        assert!(result.is_some());
        match result.unwrap() {
            uc_engine::AgentEventType::ToolInvoked {
                task_id,
                subtask_id,
                tool_name,
                tool_input,
            } => {
                assert_eq!(task_id.0, "t-1");
                assert_eq!(subtask_id.0, "st-1");
                assert_eq!(tool_name, "grep");
                assert_eq!(tool_input, "pattern");
            }
            _ => panic!("Expected ToolInvoked"),
        }
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_subtask_progress() {
        let mut data = serde_json::Map::new();
        data.insert(
            "worker_id".to_string(),
            serde_json::Value::String("w-1".to_string()),
        );
        data.insert(
            "phase".to_string(),
            serde_json::Value::String("step 2/3: codex".to_string()),
        );
        // percent as a JSON number
        data.insert("percent".to_string(), serde_json::Value::Number(50.into()));
        // step_index/step_total as numbers
        data.insert(
            "step_index".to_string(),
            serde_json::Value::Number(2.into()),
        );
        data.insert(
            "step_total".to_string(),
            serde_json::Value::Number(3.into()),
        );
        data.insert(
            "step_agent".to_string(),
            serde_json::Value::String("codex".to_string()),
        );
        data.insert(
            "step_status".to_string(),
            serde_json::Value::String("running".to_string()),
        );
        data.insert(
            "step_summary".to_string(),
            serde_json::Value::String("editing main.rs".to_string()),
        );

        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "subtask_progress".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };

        let result = nats_event_to_agent_event(&event);
        assert!(
            result.is_some(),
            "subtask_progress should not hit catch-all"
        );
        match result.unwrap() {
            uc_engine::AgentEventType::SubtaskProgress {
                task_id,
                subtask_id,
                worker_id,
                phase,
                percent,
                step_index,
                step_total,
                step_agent,
                step_status,
                step_summary,
                parallel_group: _,
                parallel_step_count: _,
            } => {
                assert_eq!(task_id.0, "t-1");
                assert_eq!(subtask_id.0, "st-1");
                assert_eq!(worker_id.0, "w-1");
                assert_eq!(phase, "step 2/3: codex");
                assert_eq!(percent, 50);
                assert_eq!(step_index, Some(2));
                assert_eq!(step_total, Some(3));
                assert_eq!(step_agent.as_deref(), Some("codex"));
                assert_eq!(step_status.as_deref(), Some("running"));
                assert_eq!(step_summary.as_deref(), Some("editing main.rs"));
            }
            _ => panic!("Expected SubtaskProgress"),
        }
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_event_to_agent_event_subtask_progress_stringified_percent() {
        // percent may arrive as a stringified number (gRPC data map is map<string,string>)
        let mut data = serde_json::Map::new();
        data.insert(
            "worker_id".to_string(),
            serde_json::Value::String("w-1".to_string()),
        );
        data.insert(
            "phase".to_string(),
            serde_json::Value::String("executing".to_string()),
        );
        data.insert(
            "percent".to_string(),
            serde_json::Value::String("50".to_string()),
        );

        let event = NatsTaskEvent {
            v: default_event_version(),
            message_id: None,
            r#type: "subtask_progress".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };

        let result = nats_event_to_agent_event(&event);
        assert!(result.is_some());
        match result.unwrap() {
            uc_engine::AgentEventType::SubtaskProgress {
                percent,
                step_index,
                step_total,
                step_agent,
                ..
            } => {
                assert_eq!(percent, 50, "stringified percent should parse to 50");
                assert!(step_index.is_none(), "missing step_index should be None");
                assert!(step_total.is_none());
                assert!(step_agent.is_none());
            }
            _ => panic!("Expected SubtaskProgress"),
        }
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn json_bool_or_default_bool_values() {
        let mut data = serde_json::Map::new();
        data.insert("flag".to_string(), serde_json::Value::Bool(true));
        data.insert("off".to_string(), serde_json::Value::Bool(false));

        assert!(json_bool_or_default(&data, "flag", false));
        assert!(!json_bool_or_default(&data, "off", true));
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn json_bool_or_default_string_values() {
        let mut data = serde_json::Map::new();
        data.insert(
            "flag".to_string(),
            serde_json::Value::String("true".to_string()),
        );
        data.insert(
            "off".to_string(),
            serde_json::Value::String("false".to_string()),
        );

        assert!(json_bool_or_default(&data, "flag", false));
        assert!(!json_bool_or_default(&data, "off", true));
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn json_bool_or_default_missing_key() {
        let data = serde_json::Map::new();

        // Default is returned when key is missing
        assert!(json_bool_or_default(&data, "missing", true));
        assert!(!json_bool_or_default(&data, "missing", false));
    }

    #[tokio::test]
    async fn broadcast_channel_receives_events() {
        let (tx, mut rx1) = broadcast::channel::<TaskEvent>(256);
        let mut rx2 = tx.subscribe();

        let event = TaskEvent {
            timestamp: String::new(),
            r#type: "TaskCreated".to_string(),
            task_id: "t-1".to_string(),
            subtask_id: None,
            data: HashMap::new(),
        };

        let _ = tx.send(event.clone());

        // Both subscribers receive the event
        let received1 = rx1.try_recv().unwrap();
        assert_eq!(received1.task_id, "t-1");

        let received2 = rx2.try_recv().unwrap();
        assert_eq!(received2.task_id, "t-1");
    }

    #[tokio::test]
    async fn broadcast_channel_lagged() {
        let (tx, mut rx) = broadcast::channel::<TaskEvent>(2);

        // Send 5 events — receiver capacity is 2, so some will be dropped
        for i in 0..5 {
            let event = TaskEvent {
                timestamp: String::new(),
                r#type: "TaskCreated".to_string(),
                task_id: format!("t-{i}"),
                subtask_id: None,
                data: HashMap::new(),
            };
            let _ = tx.send(event);
        }

        // Receiver should get a Lagged error when trying to recv
        let result = rx.try_recv();
        assert!(
            result.is_ok() || matches!(result, Err(broadcast::error::TryRecvError::Lagged(_))),
            "Expected Ok or Lagged, got {:?}",
            result
        );
    }

    #[tokio::test]
    async fn watch_task_stream_receives_broadcast() {
        use uc_engine::LocalEngine;

        let engine = LocalEngine::new_fallback();
        let server = GrpcServer::new(engine);
        let tx = server.inner.event_tx.clone();

        // Subscribe BEFORE sending — broadcast only delivers to active receivers
        let mut rx = tx.subscribe();

        let event = TaskEvent {
            timestamp: String::new(),
            r#type: "SubtaskAssigned".to_string(),
            task_id: "t-broadcast".to_string(),
            subtask_id: Some("st-1".to_string()),
            data: HashMap::new(),
        };
        let _ = tx.send(event);

        let received = rx.recv().await.unwrap();
        assert_eq!(received.task_id, "t-broadcast");
        assert_eq!(received.r#type, "SubtaskAssigned");
    }

    #[test]
    fn grpc_server_new_is_sync() {
        // Verify GrpcServer::new() is not async — compile-time check
        fn _assert_sync<T: Sync>() {}
        fn _check<E: EngineApi + Send + Sync + 'static>(engine: E) {
            let _server = GrpcServer::new(engine);
        }
        // If this compiles, new() is sync
        _assert_sync::<GrpcServer<uc_engine::LocalEngine>>();
    }

    // ── ExecutionScope: submit validation (T8 #650 / D8 #645) ────

    #[tokio::test]
    async fn submit_task_rejects_empty_and_blank_project_id() {
        use uc_engine::LocalEngine;

        let server = GrpcServer::new(LocalEngine::new_fallback());

        for project_id in ["", "   ", "\t\n"] {
            let resp = server
                .submit_task(tonic::Request::new(SubmitTaskRequest {
                    description: "Scoped task".to_string(),
                    project_id: project_id.to_string(),
                }))
                .await
                .unwrap()
                .into_inner();
            assert!(
                !resp.success,
                "empty/blank project_id must be rejected: {project_id:?}"
            );
            let err = resp.error.unwrap_or_default();
            assert!(
                err.contains("project_id cannot be empty"),
                "error must name the violated invariant, got: {err}"
            );
            assert!(resp.task_id.is_empty(), "no task may be created");
        }
    }

    #[tokio::test]
    async fn submit_task_accepts_concrete_project_id() {
        use uc_engine::LocalEngine;

        // Non-messaging fallback path: submit_task decomposes locally.
        let server = GrpcServer::new(LocalEngine::new_fallback());
        let resp = server
            .submit_task(tonic::Request::new(SubmitTaskRequest {
                description: "Scoped task".to_string(),
                project_id: "proj-1".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.success, "{:?}", resp.error);
        let store = server.inner.task_store.lock().await;
        let task = store.get_task(&resp.task_id).expect("task created");
        assert_eq!(task.project_id, "proj-1");
    }

    // ── NATS task update broadcast tests ────────────────────────

    #[tokio::test]
    async fn nats_task_update_broadcasts_subtask_events() {
        let (event_tx, _) = broadcast::channel::<TaskEvent>(256);
        let task_store = Arc::new(Mutex::new(TaskStore::new()));
        let mut rx = event_tx.subscribe();

        // First create a task with a subtask
        let task_id;
        let subtask_id;
        {
            let mut store = task_store.lock().await;
            let task = store.submit_task("Test task".to_string(), "p1".to_string());
            task_id = task.id.0.clone();
            subtask_id = task.subtasks[0].id.0.clone();
        }

        // Apply a NATS update that changes subtask status to Completed
        {
            let mut store = task_store.lock().await;
            let update = NatsTaskUpdate {
                message_id: None,
                task_id: task_id.clone(),
                status: "InProgress".to_string(),
                partial: false,
                subtasks: vec![NatsSubtaskUpdate {
                    subtask_id: subtask_id.clone(),
                    status: "Completed".to_string(),
                    assigned_worker: None,
                    description: None,
                    depends_on: None,
                    result: None,
                    attempt_id: None,
                    usage: None,
                    steps: None,

                    review: None,
                }],
                result: None,
            };
            store.apply_update(&update);
        }

        // Record events for the subtask status transition (mirrors the NATS subscriber logic)
        // Clone needed data first to avoid borrow conflict
        {
            let mut store = task_store.lock().await;
            let event_data: Option<(uc_types::TaskId, uc_types::TaskId)> =
                if let Some(task) = store.tasks.get(&task_id) {
                    if let Some(subtask) = task.subtasks.iter().find(|st| st.id.0 == subtask_id) {
                        if subtask.status == uc_types::SubtaskStatus::Completed {
                            Some((task.id.clone(), subtask.id.clone()))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                } else {
                    None
                };

            if let Some((tid, sid)) = event_data {
                store.record_event(uc_engine::AgentEventType::SubtaskCompleted {
                    task_id: tid,
                    subtask_id: sid,
                    summary: String::new(),
                    success: true,
                    modified_files: Vec::new(),
                    output: String::new(),
                    simulated: false,
                });
            }
        }

        // Broadcast the new event
        {
            let store = task_store.lock().await;
            let events: Vec<TaskEvent> = store.events.iter().cloned().map(|e| e.into()).collect();
            for event in events {
                let _ = event_tx.send(event);
            }
        }

        // The receiver should get SubtaskCompleted events
        let mut found_completed = false;
        while let Ok(event) = rx.try_recv() {
            if event.r#type == "subtask_completed" {
                found_completed = true;
            }
        }
        assert!(found_completed, "Expected SubtaskCompleted broadcast event");
    }

    #[tokio::test]
    async fn nats_task_update_applies_and_broadcasts() {
        // End-to-end test: apply a NATS update (like the subscriber does)
        // and verify events are broadcast.
        let (event_tx, _) = broadcast::channel::<TaskEvent>(256);
        let task_store = Arc::new(Mutex::new(TaskStore::new()));
        let mut rx = event_tx.subscribe();

        // Create a task
        let task_id;
        let subtask_id;
        {
            let mut store = task_store.lock().await;
            let task = store.submit_task("Test task".to_string(), "p1".to_string());
            task_id = task.id.0.clone();
            subtask_id = task.subtasks[0].id.0.clone();
        }

        // Simulate what the NATS subscriber does: apply_update + record events + broadcast
        {
            let mut store = task_store.lock().await;
            let update = NatsTaskUpdate {
                message_id: None,
                task_id: task_id.clone(),
                status: "InProgress".to_string(),
                partial: false,
                subtasks: vec![NatsSubtaskUpdate {
                    subtask_id: subtask_id.clone(),
                    status: "Assigned".to_string(),
                    assigned_worker: Some("worker-1".to_string()),
                    description: None,
                    depends_on: None,
                    result: None,
                    attempt_id: None,
                    usage: None,
                    steps: None,

                    review: None,
                }],
                result: None,
            };
            store.apply_update(&update);
        }

        // Record subtask event (mirrors subscriber logic) and capture it for
        // broadcast (decoupled from the inline log index, which is unreliable
        // at capacity — the production fix builds proto from the collected event).
        let agent_event_to_broadcast: Option<uc_engine::AgentEventType> = {
            let mut store = task_store.lock().await;
            let event_data: Option<(uc_types::TaskId, uc_types::TaskId, uc_types::WorkerId)> =
                if let Some(task) = store.tasks.get(&task_id) {
                    if let Some(subtask) = task.subtasks.iter().find(|st| st.id.0 == subtask_id) {
                        if subtask.status == uc_types::SubtaskStatus::Assigned {
                            Some((
                                task.id.clone(),
                                subtask.id.clone(),
                                subtask.assigned_worker.clone().unwrap_or_default(),
                            ))
                        } else {
                            None
                        }
                    } else {
                        None
                    }
                } else {
                    None
                };

            event_data.map(|(tid, sid, wid)| {
                let ev = uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: tid,
                    subtask_id: sid,
                    worker_id: wid,
                };
                store.record_event(ev.clone());
                ev
            })
        };

        // Broadcast the captured event (mirrors production: build proto directly
        // from the collected event, not from self.events[index..]).
        if let Some(ev) = agent_event_to_broadcast {
            let proto: TaskEvent = ev.into();
            let _ = event_tx.send(proto);
        }

        // Verify broadcast
        let mut found_assigned = false;
        while let Ok(event) = rx.try_recv() {
            if event.r#type == "subtask_assigned" {
                found_assigned = true;
            }
        }
        assert!(
            found_assigned,
            "Expected SubtaskAssigned broadcast event from NATS update"
        );
    }

    // ── Dedup tests ─────────────────────────────────────────────

    #[test]
    fn dedup_none_message_id_always_processed() {
        let mut store = TaskStore::new();
        // No message_id — should never be considered a duplicate
        assert!(!store.check_and_record_message_id(&None));
        assert!(!store.check_and_record_message_id(&None));
        assert!(!store.check_and_record_message_id(&None));
    }

    #[test]
    fn dedup_empty_message_id_always_processed() {
        let mut store = TaskStore::new();
        // Empty string message_id — should never be considered a duplicate
        assert!(!store.check_and_record_message_id(&Some(String::new())));
        assert!(!store.check_and_record_message_id(&Some(String::new())));
    }

    #[test]
    fn dedup_detects_duplicate_message_id() {
        let mut store = TaskStore::new();
        let mid = Some("t-1:subtask_assigned:st-1:1700000000".to_string());
        // First occurrence — not a duplicate
        assert!(!store.check_and_record_message_id(&mid));
        // Second occurrence — is a duplicate
        assert!(store.check_and_record_message_id(&mid));
    }

    #[test]
    fn dedup_different_message_ids_not_duplicate() {
        let mut store = TaskStore::new();
        let mid1 = Some("t-1:subtask_assigned:st-1:1700000000".to_string());
        let mid2 = Some("t-1:subtask_started:st-1:1700000001".to_string());
        assert!(!store.check_and_record_message_id(&mid1));
        assert!(!store.check_and_record_message_id(&mid2));
    }

    #[test]
    fn dedup_purges_expired_entries() {
        let mut store = TaskStore::new();
        // Fill the map beyond DEDUP_MAX_ENTRIES with distinct keys
        for i in 0..=TaskStore::DEDUP_MAX_ENTRIES {
            let mid = Some(format!("t-1:event:st-{i}:1700000000"));
            assert!(!store.check_and_record_message_id(&mid));
        }
        // The map should have been purged — early entries may have been removed
        // but the map size should not exceed DEDUP_MAX_ENTRIES by much
        assert!(store.seen_messages.len() <= TaskStore::DEDUP_MAX_ENTRIES + 100);
    }

    #[test]
    fn inline_event_log_is_capped() {
        // Regression: the inline event log (self.events) grew unbounded on a
        // long-running server, eventually OOM-ing and crashing the gRPC server
        // (session interruption). record_event now caps it at INLINE_EVENTS_MAX,
        // keeping the most recent events (full history is in the EventStore).
        let mut store = TaskStore::new();
        // Record well beyond the cap.
        for i in 0..(TaskStore::INLINE_EVENTS_MAX + 500) {
            store.record_event(uc_engine::AgentEventType::TaskCreated {
                task_id: uc_types::TaskId(format!("t-{i}")),
                description: format!("task {i}"),
            });
        }
        assert!(
            store.events.len() <= TaskStore::INLINE_EVENTS_MAX,
            "inline event log exceeded cap: {} > {}",
            store.events.len(),
            TaskStore::INLINE_EVENTS_MAX,
        );
        // The most recent event must be retained (not the oldest).
        match store.events.last() {
            Some(uc_engine::AgentEventType::TaskCreated { task_id, .. }) => {
                assert!(task_id.0.starts_with("t-"));
            }
            other => panic!("expected last event to be TaskCreated, got {:?}", other),
        }
    }

    #[test]
    fn record_event_with_subject_returns_proto_even_at_capacity() {
        // Regression: the broadcast used events[event_count_before..] to extract
        // newly-recorded events. When the inline log was at capacity, record_event
        // drained oldest entries (keeping len == cap), so the slice was empty and
        // the broadcast silently dropped. The fix: record_event_with_subject
        // returns the proto TaskEvent directly, decoupling broadcast from the
        // unreliable index.
        let mut store = TaskStore::new();
        // Fill the log to capacity.
        for i in 0..TaskStore::INLINE_EVENTS_MAX {
            store.record_event_with_subject(
                uc_engine::AgentEventType::TaskCreated {
                    task_id: uc_types::TaskId(format!("fill-{i}")),
                    description: String::new(),
                },
                "task.fill",
            );
        }
        assert_eq!(store.events.len(), TaskStore::INLINE_EVENTS_MAX);

        // Record one more — triggers drain, but the returned proto must still
        // represent the new event (not be empty/dropped).
        let proto = store.record_event_with_subject(
            uc_engine::AgentEventType::TaskCreated {
                task_id: uc_types::TaskId("new-after-cap".to_string()),
                description: String::new(),
            },
            "task.new",
        );
        // The returned proto is broadcastable — non-default type.
        assert!(
            !proto.r#type.is_empty(),
            "record_event_with_subject returned empty proto — broadcast would be dropped at cap"
        );
        // The inline log stays capped.
        assert_eq!(store.events.len(), TaskStore::INLINE_EVENTS_MAX);
    }

    #[test]
    fn task_map_evicts_terminal_tasks_beyond_cap() {
        // Regression: the in-memory tasks HashMap grew unbounded on a long-
        // running gRPC server (OOM → crash → session interruption). Terminal
        // tasks now evict when the map exceeds MAX_RETAINED_TASKS.
        let mut store = TaskStore::new();
        // Submit well beyond the cap, marking each Completed.
        for _ in 0..(TaskStore::MAX_RETAINED_TASKS + 50) {
            let (task, _) = store.submit_task_pending("t".to_string(), "p".to_string());
            store.set_task_status(&task.id.0, uc_types::TaskStatus::Completed);
            // apply_update's evict path also runs here for completeness.
            store.evict_completed_tasks();
        }
        assert!(
            store.tasks.len() <= TaskStore::MAX_RETAINED_TASKS,
            "task map exceeded cap: {} > {}",
            store.tasks.len(),
            TaskStore::MAX_RETAINED_TASKS,
        );
        // Non-terminal tasks must never be evicted.
        for t in store.tasks.values() {
            assert!(
                !matches!(
                    t.status,
                    uc_types::TaskStatus::InProgress | uc_types::TaskStatus::Planning
                ),
                "non-terminal task evicted: {:?}",
                t.status,
            );
        }
    }

    #[test]
    fn task_map_evict_noop_under_cap() {
        let mut store = TaskStore::new();
        store.submit_task_pending("t".to_string(), "p".to_string());
        assert_eq!(store.evict_completed_tasks(), 0);
    }

    #[test]
    fn update_task_rejects_unknown_status() {
        // Regression: update_task used `if let Ok(parsed) = ...` which silently
        // dropped an unrecognized status string, keeping the old status while
        // still refreshing updated_at — masked as a successful no-op. Must now
        // return Err naming the bad status, with task untouched.
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("t".to_string(), "p".to_string());
        let before = store.tasks.get(&task.id.0).unwrap().updated_at;
        let result = store.update_task(
            &task.id.0,
            "BogusStatus",
            vec![],
            "", // empty description → skip create-if-not-exists
            "",
        );
        let err = result.expect_err("unknown status must Err");
        assert!(
            err.contains("BogusStatus"),
            "err must name the bad status: {err}"
        );
        let after = store.tasks.get(&task.id.0).unwrap();
        assert_eq!(
            after.updated_at, before,
            "updated_at must NOT refresh on rejected status"
        );
        assert_eq!(after.status, uc_types::TaskStatus::Planning);
    }

    #[test]
    fn evict_completed_tasks_clears_assigned_subtask_tracking() {
        // Defensive: evict_completed_tasks clears residual assigned_subtask_times
        // entries for evicted tasks' subtasks, so a future path that removes a
        // task without going through update_subtask_status can't leak tracking.
        let mut store = TaskStore::new();
        // Force eviction by exceeding MAX_RETAINED_TASKS with terminal tasks,
        // one of which has a residual assigned_subtask_times entry.
        for i in 0..(TaskStore::MAX_RETAINED_TASKS + 5) {
            let task = store.submit_task(format!("t{i}"), "p".to_string());
            store.set_task_status(&task.id.0, uc_types::TaskStatus::Completed);
            // Simulate a residual tracking entry on the first task's subtask.
            if i == 0 {
                let st_id = task.subtasks[0].id.0.clone();
                store
                    .assigned_subtask_times
                    .insert(st_id, chrono::Utc::now());
            }
        }
        let evicted = store.evict_completed_tasks();
        assert!(evicted > 0);
        // All assigned_subtask_times entries for evicted tasks' subtasks cleared.
        // (Residual entries can only belong to evicted tasks here since terminal
        // tasks' subtasks are non-Assigned by construction.)
        assert!(
            store.assigned_subtask_times.len() < TaskStore::MAX_RETAINED_TASKS,
            "assigned_subtask_times not cleared on eviction"
        );
    }

    #[test]
    fn nats_task_update_backward_compat_no_message_id() {
        // Verify that NatsTaskUpdate JSON without message_id deserializes correctly
        let json = r#"{"task_id":"t-1","status":"InProgress","subtasks":[],"result":null}"#;
        let parsed: NatsTaskUpdate = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.task_id, "t-1");
        assert_eq!(parsed.message_id, None);
    }

    #[test]
    fn nats_subtask_update_backward_compat_no_new_fields() {
        // Verify that old-format subtask JSON (without description/depends_on)
        // deserializes with defaults, ensuring backward compatibility.
        let json = r#"{"subtask_id":"st-1","status":"Assigned","assigned_worker":"w-1"}"#;
        let parsed: NatsSubtaskUpdate = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.subtask_id, "st-1");
        assert_eq!(parsed.status, "Assigned");
        assert_eq!(parsed.assigned_worker, Some("w-1".to_string()));
        assert_eq!(parsed.description, None);
        assert_eq!(parsed.depends_on, None);
        assert_eq!(parsed.result, None);
    }

    #[test]
    fn apply_update_full_upsert_existing_subtask() {
        // Verify that apply_update updates description, depends_on, and result
        // on an existing subtask when provided in the update.
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();
        let subtask_id = task.subtasks[0].id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "InProgress".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: Some("Updated description".to_string()),
                depends_on: Some(vec!["other-st".to_string()]),
                result: Some("Work done".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        let st = &updated.subtasks[0];
        assert_eq!(st.description, "Updated description");
        assert_eq!(st.depends_on.len(), 1);
        assert_eq!(st.depends_on[0].0, "other-st");
        assert!(st.result.is_some());
        assert_eq!(st.result.as_ref().unwrap().summary, "Work done");
    }

    #[test]
    fn apply_update_full_upsert_new_subtask() {
        // Verify that apply_update creates a new subtask with description,
        // depends_on, and result from the update payload.
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-new".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-2".to_string()),
                description: Some("New subtask from Python".to_string()),
                depends_on: Some(vec!["dep-1".to_string(), "dep-2".to_string()]),
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(updated.subtasks.len(), 1);
        let st = &updated.subtasks[0];
        assert_eq!(st.id.0, "st-new");
        assert_eq!(st.description, "New subtask from Python");
        assert_eq!(st.depends_on.len(), 2);
        assert_eq!(st.depends_on[0].0, "dep-1");
        assert_eq!(st.depends_on[1].0, "dep-2");
        assert!(st.result.is_none());
    }

    #[test]
    fn apply_update_new_failed_subtask_derives_success_false() {
        // Regression: the new-subtask path hardcoded success: true on the
        // SubtaskResult, so a failed subtask arriving via NATS update (with a
        // result) was recorded as successful — diverging from the existing-
        // subtask path which derives success from status. Success must be
        // derived from status in both paths.
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-failed-new".to_string(),
                status: "Failed".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: Some("Failed new subtask".to_string()),
                depends_on: None,
                result: Some("error: something broke".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        let st = &updated.subtasks[0];
        let result = st.result.as_ref().expect("result should be set");
        assert!(
            !result.success,
            "failed new subtask must record success=false, not hardcoded true"
        );
        assert_eq!(result.summary, "error: something broke");
    }

    #[test]
    fn apply_update_clears_assigned_tracking_on_transition_out() {
        // Regression: apply_update set subtask.status directly (bypassing
        // update_subtask_status), so the assigned_subtask_times entry leaked on
        // every Assigned→InProgress transition — the highest-frequency path
        // (worker pick-up). Now apply_update clears/records tracking to mirror
        // update_subtask_status.
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();

        // Mark Assigned via update_subtask_status (records tracking).
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        assert!(store.assigned_subtask_times.contains_key(&st_id));

        // Simulate worker pick-up: NATS update Assigned → InProgress.
        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "InProgress".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };
        store.apply_update(&update);

        // Tracking entry must be cleared (not leaked) on Assigned→InProgress.
        assert!(
            !store.assigned_subtask_times.contains_key(&st_id),
            "assigned_subtask_times entry leaked on Assigned→InProgress via apply_update"
        );
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::InProgress);
    }

    #[test]
    fn apply_update_backward_compat_old_format() {
        // Verify that old-format updates (without description/depends_on)
        // still work — description defaults to empty, depends_on defaults to empty vec.
        let mut store = TaskStore::new();
        let (task, _) = store.submit_task_pending("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-old".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: None,
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };

        store.apply_update(&update);

        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(updated.subtasks.len(), 1);
        let st = &updated.subtasks[0];
        assert_eq!(st.description, "");
        assert!(st.depends_on.is_empty());
        assert!(st.result.is_none());
    }

    #[test]
    fn nats_task_event_backward_compat_no_message_id() {
        // Verify that NatsTaskEvent JSON without message_id deserializes correctly
        let json = r#"{"type":"tool_call","task_id":"t-1","subtask_id":"st-1","data":{}}"#;
        let parsed: NatsTaskEvent = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.r#type, "tool_call");
        assert_eq!(parsed.message_id, None);
    }

    #[test]
    fn nats_subtask_execute_serializes_steps_and_agent_config_json() {
        // Wire contract: the JSON the Python worker consumes must use
        // snake_case keys, `agent_config_json` (string), and a `steps` array
        // whose items themselves use `agent_config_json`. Any rename here
        // silently breaks _resolve_agent_config_field / WorkflowStep.from_dict
        // on the Python side (covered by test_workflow_orchestration.py).
        let payload = NatsSubtaskExecute {
            message_id: Some("m1".to_string()),
            description: "implement X".to_string(),
            expected_output: "code".to_string(),
            file_constraints: Vec::new(),
            timeout_seconds: 600,
            retry_count: 0,
            dispatch_mode: uc_types::DispatchMode::PreferRemote,
            effect_class: uc_types::EffectClass::default(),
            required_capabilities: Vec::new(),
            agent_config_json: Some(r#"{"agent_name":"coder"}"#.to_string()),
            steps: vec![uc_types::WorkflowStep {
                agent: "codex".to_string(),
                prompt: "CR {{prev_summary}}".to_string(),
                agent_config_json: Some(r#"{"agent_name":"reviewer"}"#.to_string()),
                abort_on_failure: false,
                retry_count: 0,
                retry_delay_ms: 0,
                condition: None,
                parallel_group: None,
            }],
            project_id: "proj-1".to_string(),
            graph_id: "t-1".to_string(),
            node_id: "st-1".to_string(),
            attempt_id: "0".to_string(),
            idempotency_key: "00000000000000000000000000000000".to_string(),
            worker_epoch: String::new(),
            contract_version: uc_types::CONTRACT_VERSION.to_string(),
            context_block: None,
        };
        let json = serde_json::to_string(&payload).unwrap();
        // Subtask-level override key is agent_config_json (not agent_config).
        assert!(json.contains("\"agent_config_json\""));
        // steps array is emitted.
        assert!(json.contains("\"steps\""));
        // DispatchMode serializes as the variant name (PascalCase) — the Python
        // side's _dispatch_mode_from_payload handles this case-insensitively.
        assert!(json.contains("\"PreferRemote\""));
        // Round-trips back losslessly (identity rides the envelope — the
        // legacy task_id/subtask_id keys are gone since T6 #642).
        let back: NatsSubtaskExecute = serde_json::from_str(&json).unwrap();
        assert_eq!(back.graph_id, "t-1");
        assert_eq!(back.node_id, "st-1");
        assert_eq!(back.steps.len(), 1);
        assert_eq!(back.steps[0].agent, "codex");
        assert!(!back.steps[0].abort_on_failure);
        assert_eq!(
            back.agent_config_json.as_deref(),
            Some(r#"{"agent_name":"coder"}"#)
        );
    }

    #[test]
    fn nats_subtask_execute_backward_compat_no_steps_no_agent_config() {
        // A legacy payload (pre-workflow) has neither steps nor agent_config_json.
        // Both must deserialize to empty/None without error.
        let json = r#"{
            "task_id": "t-1",
            "subtask_id": "st-1",
            "description": "legacy",
            "expected_output": "",
            "file_constraints": [],
            "timeout_seconds": 600,
            "retry_count": 0,
            "dispatch_mode": "PreferRemote",
            "required_capabilities": [],
            "project_id": ""
        }"#;
        let parsed: NatsSubtaskExecute = serde_json::from_str(json).unwrap();
        assert!(parsed.steps.is_empty());
        assert_eq!(parsed.agent_config_json, None);
        assert_eq!(parsed.message_id, None);
    }

    #[test]
    fn nats_subtask_execute_envelope_roundtrip_and_legacy_tolerance() {
        // T1 #637: the six envelope keys ride on every dispatch payload;
        // ALL of them are serde(default), so a pre-envelope (legacy) payload
        // still parses with empty envelope fields instead of failing.
        let with_envelope = r#"{
            "task_id": "t-1", "subtask_id": "st-1", "description": "x",
            "graph_id": "t-1", "node_id": "st-1", "attempt_id": "2",
            "idempotency_key": "abc", "worker_epoch": "", "contract_version": "v1"
        }"#;
        let parsed: NatsSubtaskExecute = serde_json::from_str(with_envelope).unwrap();
        assert_eq!(parsed.graph_id, "t-1");
        assert_eq!(parsed.node_id, "st-1");
        assert_eq!(parsed.attempt_id, "2");
        assert_eq!(parsed.idempotency_key, "abc");
        assert_eq!(parsed.worker_epoch, "");
        assert_eq!(parsed.contract_version, "v1");

        let legacy = r#"{"task_id":"t-1","subtask_id":"st-1","description":"x"}"#;
        let parsed: NatsSubtaskExecute = serde_json::from_str(legacy).unwrap();
        assert_eq!(parsed.graph_id, "");
        assert_eq!(parsed.contract_version, "");
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn subtask_execute_payload_emits_deterministic_envelope() {
        // Both gateway publishers build through subtask_execute_payload; the
        // envelope must carry the identity mapping and be byte-deterministic
        // for the same (graph,node,attempt) — only message_id varies.
        let st = uc_types::Subtask {
            id: uc_types::TaskId("st-7".into()),
            parent_id: uc_types::TaskId("t-3".into()),
            description: "d".into(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: vec![],
            file_constraints: vec!["*.lock".into()],
            expected_output: "out".into(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 2,
            required_capabilities: vec![],
            agent_config_json: None,
            steps: vec![],
            retry_count: 0,
        };
        let p1 = subtask_execute_payload("t-3", &st, "proj", "out", &st.file_constraints, None);
        let p2 = subtask_execute_payload("t-3", &st, "proj", "out", &st.file_constraints, None);
        assert_eq!(p1.graph_id, "t-3");
        assert_eq!(p1.node_id, "st-7");
        assert_eq!(p1.attempt_id, "2"); // identity mapping: attempt = dispatch_retry_count
        assert_eq!(p1.worker_epoch, "");
        assert_eq!(p1.contract_version, uc_types::CONTRACT_VERSION);
        let expected_key = uc_types::ExecutionEnvelope::derive_idempotency_key("t-3", "st-7", "2");
        assert_eq!(p1.idempotency_key, expected_key);
        // T4 #640 (D3 lockstep): legacy identity keys are gone from the wire —
        // workers read graph_id/node_id from the envelope.
        let wire = serde_json::to_value(&p1).unwrap();
        assert!(
            wire.get("task_id").is_none(),
            "legacy task_id must not be emitted"
        );
        assert!(
            wire.get("subtask_id").is_none(),
            "legacy subtask_id must not be emitted"
        );
        // T5 #641 (D4 Q2): effect_class rides the dispatch payload
        // (snake_case, additive) so workers can honor local-execution
        // eligibility without a schema bump.
        assert_eq!(wire["effect_class"], "requires_worker");

        // Determinism: same triple → envelope half of the payload is
        // byte-identical across builds (message_id deliberately excluded —
        // it is the millis timestamp T4 will replace with the key).
        let mut a = p1.clone();
        let mut b = p2.clone();
        a.message_id = None;
        b.message_id = None;
        assert_eq!(
            serde_json::to_vec(&a).unwrap(),
            serde_json::to_vec(&b).unwrap()
        );

        // T4 #640: the dispatch carries that deterministic key as
        // `Nats-Msg-Id`, which is what makes the stream's duplicate_window
        // collapse re-sends. Same triple → same header, so a re-dispatch
        // (retry / re-publish / gateway restart) never reaches a worker
        // twice; a different attempt → a different header → it does.
        let headers = dispatch_dedup_headers(&p1.idempotency_key);
        assert_eq!(
            headers.get("Nats-Msg-Id").map(|v| v.as_str()),
            Some(expected_key.as_str()),
            "dispatch must carry the idempotency key as Nats-Msg-Id"
        );
        let mut st_next = st.clone();
        st_next.dispatch_retry_count = 3;
        let p3 = subtask_execute_payload("t-3", &st_next, "proj", "out", &[], None);
        let next_headers = dispatch_dedup_headers(&p3.idempotency_key);
        assert_ne!(
            next_headers.get("Nats-Msg-Id").map(|v| v.as_str()),
            headers.get("Nats-Msg-Id").map(|v| v.as_str()),
            "a new attempt must NOT be deduped against the previous one — \
             that would break T3's fence → READY → re-dispatch"
        );
    }

    // ── T12 #654 — affinity placement: targeted vs overflow subject ──

    /// A registry holding one signalled worker. `topic == false` models a
    /// legacy worker (shared consumer only).
    #[cfg(feature = "messaging")]
    fn registry_with(
        id: &str,
        host: &str,
        load: u32,
        recent: &[&str],
        topic: bool,
    ) -> crate::worker_service::WorkerRegistry {
        let mut reg = crate::worker_service::WorkerRegistry::new();
        reg.register(
            id.to_string(),
            vec!["code".to_string()],
            4,
            format!(r#"{{"hostname":"{host}"}}"#),
            uc_types::CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        let files: Vec<String> = recent.iter().map(|s| s.to_string()).collect();
        reg.heartbeat_with_signals(id, load, &files, topic).unwrap();
        reg
    }

    #[cfg(feature = "messaging")]
    fn subtask_with_files(files: &[&str]) -> uc_types::Subtask {
        uc_types::Subtask {
            id: uc_types::TaskId("st-1".into()),
            parent_id: uc_types::TaskId("t-1".into()),
            description: "d".into(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: vec![],
            file_constraints: files.iter().map(|s| s.to_string()).collect(),
            expected_output: "out".into(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            required_capabilities: vec!["code".into()],
            agent_config_json: None,
            steps: vec![],
            retry_count: 0,
        }
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn placement_targets_the_overlapping_workers_subject_not_the_shared_queue() {
        // Acceptance (#654): a node whose file_constraints overlap worker A's
        // recent files lands on A's per-worker subject, not the shared queue.
        let reg = registry_with("w-a", "box-a", 1, &["src/auth.rs"], true);
        let subject = resolve_dispatch_subject(
            &reg,
            &subtask_with_files(&["src/auth.rs"]),
            "",
            &std::collections::HashSet::new(),
        );
        assert_eq!(subject, "uc.subtask.execute.w.w-a");
        assert_ne!(subject, NATS_SUBJECT_SUBTASK_EXECUTE);
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn placement_falls_back_to_shared_when_nothing_overlaps() {
        // No overlap → no scoring reason to target, and overflow is strictly
        // better (any worker may claim it). Still dispatched, never dropped.
        let reg = registry_with("w-a", "box-a", 0, &["src/auth.rs"], true);
        let subject = resolve_dispatch_subject(
            &reg,
            &subtask_with_files(&["src/unrelated.rs"]),
            "",
            &std::collections::HashSet::new(),
        );
        assert_eq!(subject, NATS_SUBJECT_SUBTASK_EXECUTE);
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn legacy_worker_still_receives_via_overflow() {
        // A worker that never bound a per-worker consumer is never targeted —
        // it could not consume the targeted publish, so the node would be
        // stranded until redelivery. It still gets work from the shared queue.
        let reg = registry_with("w-legacy", "box-a", 0, &["src/auth.rs"], false);
        let subject = resolve_dispatch_subject(
            &reg,
            &subtask_with_files(&["src/auth.rs"]),
            "",
            &std::collections::HashSet::new(),
        );
        assert_eq!(subject, NATS_SUBJECT_SUBTASK_EXECUTE);
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn placement_prefers_the_host_already_running_a_sibling_node() {
        let mut reg = registry_with("w-remote", "box-x", 1, &["src/auth.rs"], true);
        // Identical affinity and load — locality is the only separating signal.
        reg.register(
            "w-local".to_string(),
            vec!["code".to_string()],
            4,
            r#"{"hostname":"box-y"}"#.to_string(),
            uc_types::CONTRACT_VERSION.to_string(),
        )
        .unwrap();
        reg.heartbeat_with_signals("w-local", 1, &["src/auth.rs".to_string()], true)
            .unwrap();

        let sibling_hosts: std::collections::HashSet<String> =
            ["box-y".to_string()].into_iter().collect();
        let subject = resolve_dispatch_subject(
            &reg,
            &subtask_with_files(&["src/auth.rs"]),
            "",
            &sibling_hosts,
        );
        assert_eq!(subject, "uc.subtask.execute.w.w-local");
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn placement_never_targets_across_the_capability_gate() {
        // A worker with no overlap AND a capability the node requires that it
        // lacks is not a candidate — the hard gate runs before scoring.
        let reg = registry_with("w-a", "box-a", 0, &["src/auth.rs"], true);
        let mut st = subtask_with_files(&["src/auth.rs"]);
        st.required_capabilities = vec!["rust".into()];
        let subject = resolve_dispatch_subject(&reg, &st, "", &std::collections::HashSet::new());
        assert_eq!(subject, NATS_SUBJECT_SUBTASK_EXECUTE);
    }

    // ── T10 #652 — context block: payload presence + composition ────

    #[cfg(feature = "messaging")]
    #[test]
    fn subtask_execute_payload_carries_context_block() {
        let st = uc_types::Subtask {
            id: uc_types::TaskId("st-7".into()),
            parent_id: uc_types::TaskId("t-3".into()),
            description: "d".into(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: vec![],
            file_constraints: vec![],
            expected_output: "out".into(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            required_capabilities: vec![],
            agent_config_json: None,
            steps: vec![],
            retry_count: 0,
        };
        let block = uc_types::ContextBlock::compose(vec![uc_types::ContextEntry {
            node_id: "n-1".into(),
            success: true,
            summary: "done".into(),
        }])
        .unwrap();
        let p = subtask_execute_payload("t-3", &st, "proj", "out", &[], Some(block.clone()));
        assert_eq!(p.context_block.as_ref(), Some(&block));
        let wire = serde_json::to_value(&p).unwrap();
        assert_eq!(wire["context_block"]["entries"][0]["node_id"], "n-1");
        assert_eq!(wire["context_block"]["entries"][0]["summary"], "done");
        assert_eq!(wire["context_block"]["truncated"], false);

        // No context → the key is omitted from the wire entirely
        // (additive field, legacy workers never see it).
        let p0 = subtask_execute_payload("t-3", &st, "proj", "out", &[], None);
        assert!(p0.context_block.is_none());
        let wire0 = serde_json::to_value(&p0).unwrap();
        assert!(
            wire0.get("context_block").is_none(),
            "context_block must be absent (not null) when nothing was composed"
        );
    }

    #[cfg(feature = "messaging")]
    #[tokio::test]
    async fn compose_context_block_degrades_to_none_without_graph_plane() {
        let (store, _backend, _events) = make_store_with_backend();
        let store = Arc::new(Mutex::new(store));
        // No deps → None without touching the (absent) sink.
        assert!(compose_context_block(&store, "t-1", &[]).await.is_none());
        // Deps but no graph shadow → fail-soft None (never a failed dispatch).
        let deps = vec!["n-1".to_string(), "n-2".to_string()];
        assert!(compose_context_block(&store, "t-1", &deps).await.is_none());
    }

    // ── task_backend write-path persistence tests ──────────────

    /// Helper: build a TaskStore backed by InMemoryTaskBackend + InMemoryEventStore.
    fn make_store_with_backend() -> (
        TaskStore,
        Arc<uc_engine::InMemoryTaskBackend>,
        Arc<uc_engine::InMemoryEventStore>,
    ) {
        let backend = Arc::new(uc_engine::InMemoryTaskBackend::new());
        let event_store = Arc::new(uc_engine::InMemoryEventStore::new());
        let store = TaskStore::with_backend(backend.clone(), event_store.clone());
        (store, backend, event_store)
    }

    /// submit_task persists the new task to the backend via submit_task (INSERT).
    #[tokio::test]
    async fn task_backend_persists_submit_task() {
        let (mut store, backend, _es) = make_store_with_backend();

        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Yield so the fire-and-forget spawn completes.
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.description, "Test task");
        assert_eq!(got.status, uc_types::TaskStatus::InProgress);
        assert_eq!(got.subtasks.len(), 1);
    }

    /// submit_task_pending persists the new task to the backend.
    #[tokio::test]
    async fn task_backend_persists_submit_task_pending() {
        let (mut store, backend, _es) = make_store_with_backend();

        let (task, _) = store.submit_task_pending("Pending task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.description, "Pending task");
        assert_eq!(got.status, uc_types::TaskStatus::Planning);
        assert!(got.subtasks.is_empty());
    }

    /// pause_task persists the status change to the backend.
    #[tokio::test]
    async fn task_backend_persists_pause_task() {
        let (mut store, backend, _es) = make_store_with_backend();

        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Yield for submit_task persist.
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        store.pause_task(&task_id).unwrap();

        // Yield for pause_task persist.
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.status, uc_types::TaskStatus::Paused);
    }

    /// resume_task persists the status change to the backend.
    #[tokio::test]
    async fn task_backend_persists_resume_task() {
        let (mut store, backend, _es) = make_store_with_backend();

        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        store.pause_task(&task_id).unwrap();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        store.resume_task(&task_id).unwrap();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.status, uc_types::TaskStatus::InProgress);
    }

    /// cancel_task persists the status change (Failed) to the backend.
    #[tokio::test]
    async fn task_backend_persists_cancel_task() {
        let (mut store, backend, _es) = make_store_with_backend();

        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        store.cancel_task(&task_id).unwrap();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.status, uc_types::TaskStatus::Failed);
        // Subtask should also be Failed in the persisted copy.
        assert_eq!(got.subtasks.len(), 1);
        assert_eq!(got.subtasks[0].status, uc_types::SubtaskStatus::Failed);
    }

    /// update_task persists both create-if-not-exists (INSERT) and update (UPDATE) paths.
    #[tokio::test]
    async fn task_backend_persists_update_task() {
        let (mut store, backend, _es) = make_store_with_backend();

        // Create-if-not-exists path: task doesn't exist, description non-empty.
        let subtask = uc_types::Subtask {
            id: uc_types::TaskId("st-1".to_string()),
            parent_id: uc_types::TaskId("t-1".to_string()),
            description: "do thing".to_string(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            effect_class: uc_types::EffectClass::default(),
            dispatch_retry_count: 0,
            retry_count: 0,
            required_capabilities: Vec::new(),
            agent_config_json: None,
            steps: Vec::new(),
        };
        store
            .update_task(
                "t-1",
                "InProgress",
                vec![subtask],
                "New task via update",
                "p1",
            )
            .unwrap();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task("t-1").await.unwrap().unwrap();
        assert_eq!(got.description, "New task via update");
        assert_eq!(got.status, uc_types::TaskStatus::InProgress);

        // Update path: task exists, change status.
        store
            .update_task("t-1", "Paused", Vec::new(), "", "p1")
            .unwrap();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got2 = backend.get_task("t-1").await.unwrap().unwrap();
        assert_eq!(got2.status, uc_types::TaskStatus::Paused);
    }

    /// apply_update persists the mutated task to the backend.
    #[tokio::test]
    async fn task_backend_persists_apply_update() {
        let (mut store, backend, _es) = make_store_with_backend();

        let (task, _) = store.submit_task_pending("Task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "in_progress".to_string(),
            partial: false,
            subtasks: vec![],
            result: None,
        };
        store.apply_update(&update);
        tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;

        let got = backend.get_task(&task_id).await.unwrap().unwrap();
        assert_eq!(got.status, uc_types::TaskStatus::InProgress);
    }

    /// When task_backend is None (default TaskStore::new), persist is a no-op —
    /// no panic, no error. Existing behavior unchanged.
    #[tokio::test]
    async fn task_backend_none_is_noop() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        // Should not panic.
        store.pause_task(&task.id.0).unwrap();
        store.cancel_task(&task.id.0).unwrap();
    }

    /// PR3: load_from_backend recovers tasks persisted by a prior run into
    /// the in-memory HashMap (startup recovery). Simulates restart: backend
    /// has tasks, a fresh TaskStore loads them.
    #[tokio::test]
    async fn task_backend_load_from_backend_recovers_tasks() {
        let backend = Arc::new(uc_engine::InMemoryTaskBackend::new());

        // Simulate a prior run: submit tasks directly to the backend.
        let t1 = uc_types::Task {
            id: uc_types::TaskId("recover-1".to_string()),
            description: "prior task 1".to_string(),
            project_id: "p".to_string(),
            status: uc_types::TaskStatus::Paused,
            subtasks: Vec::new(),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        backend.submit_task(t1.clone()).await.unwrap();

        // Fresh TaskStore (empty HashMap) sharing the populated backend.
        let event_store = Arc::new(uc_engine::InMemoryEventStore::new());
        let mut store = TaskStore::with_backend(backend, event_store);
        assert!(
            store.get_task("recover-1").is_none(),
            "HashMap empty before load"
        );

        let count = store.load_from_backend().await.unwrap();
        assert_eq!(count, 1);
        assert_eq!(
            store.get_task("recover-1").unwrap().description,
            "prior task 1"
        );
        assert_eq!(
            store.get_task("recover-1").unwrap().status,
            uc_types::TaskStatus::Paused
        );
    }

    /// PR3: load_from_backend is a no-op (returns 0) when no backend is set.
    #[tokio::test]
    async fn task_backend_load_from_backend_none_is_noop() {
        let mut store = TaskStore::new();
        let count = store.load_from_backend().await.unwrap();
        assert_eq!(count, 0);
    }

    // ── T2 graph shadow write + T3 graph verb fan-out (fake sink, no PG) ──

    /// T7 #643 — pause-grace timer's new NATS handle: `None` under
    /// `messaging` (no client in unit tests), unit otherwise.
    fn timer_nats() -> PauseGraceNats {
        no_pause_grace_nats()
    }

    #[derive(Default)]
    struct RecordingGraphSink {
        seen: std::sync::Mutex<Vec<String>>,
        verbs: std::sync::Mutex<Vec<String>>,
        /// (graph_id, reason) of every `fail_running_attempts` call (D6).
        fail_running_calls: std::sync::Mutex<Vec<(String, String)>>,
        /// Seeded SweptAttempts the D6 verb returns (drives bridge assertions).
        fail_running_swept: std::sync::Mutex<Vec<uc_engine::SweptAttempt>>,
    }

    impl RecordingGraphSink {
        fn take_verbs(&self) -> Vec<String> {
            std::mem::take(&mut *self.verbs.lock().unwrap())
        }

        fn take_fail_running_calls(&self) -> Vec<(String, String)> {
            std::mem::take(&mut *self.fail_running_calls.lock().unwrap())
        }
    }

    #[async_trait::async_trait]
    impl uc_engine::GraphShadowSink for RecordingGraphSink {
        async fn shadow_persist(&self, task: &uc_types::Task) {
            self.seen.lock().unwrap().push(task.id.0.clone());
        }

        async fn fail_running_attempts(
            &self,
            graph_id: &str,
            reason: &str,
        ) -> Vec<uc_engine::SweptAttempt> {
            self.fail_running_calls
                .lock()
                .unwrap()
                .push((graph_id.to_string(), reason.to_string()));
            std::mem::take(&mut *self.fail_running_swept.lock().unwrap())
        }
        async fn on_schedule(&self, env: &uc_types::ExecutionEnvelope, worker: Option<&str>) {
            self.verbs.lock().unwrap().push(format!(
                "schedule {}:{}:{}",
                env.graph_id,
                env.node_id,
                worker.unwrap_or("-")
            ));
        }
        async fn on_heartbeat(&self, env: &uc_types::ExecutionEnvelope) {
            self.verbs
                .lock()
                .unwrap()
                .push(format!("heartbeat {}:{}", env.graph_id, env.node_id));
        }
        async fn on_commit(
            &self,
            env: &uc_types::ExecutionEnvelope,
            result: Option<&str>,
            usage: Option<&uc_types::SubtaskUsage>,
            steps: Option<&[uc_types::StepUsage]>,
        ) {
            let mut entry = format!(
                "commit {}:{}:{}",
                env.graph_id,
                env.node_id,
                result.unwrap_or("-")
            );
            // T15 (#660): annotate only when a usage block actually arrived, so
            // the exact strings the pre-T15 assertions encode stay identical.
            // T18 (#668) follows the same rule for the per-step records: the
            // assertion strings are the contract these tests check, and adding
            // a suffix unconditionally would make every pre-T18 expectation
            // fail for a reason that has nothing to do with the behaviour under
            // test.
            if let Some(usage) = usage {
                entry.push_str(&format!(
                    ":usage={}/{}",
                    usage
                        .total_tokens()
                        .map(|t| t.to_string())
                        .unwrap_or_else(|| "-".to_string()),
                    usage.source.clone().unwrap_or_else(|| "-".to_string())
                ));
            }
            if let Some(steps) = steps {
                entry.push_str(&format!(":steps={}", steps.len()));
            }
            self.verbs.lock().unwrap().push(entry);
        }
        async fn on_fail(&self, env: &uc_types::ExecutionEnvelope, reason: &str) {
            self.verbs
                .lock()
                .unwrap()
                .push(format!("fail {}:{}:{}", env.graph_id, env.node_id, reason));
        }
    }

    fn shadow_test_task(id: &str) -> uc_types::Task {
        uc_types::Task {
            id: uc_types::TaskId(id.to_string()),
            description: "shadow".to_string(),
            project_id: "p".to_string(),
            status: uc_types::TaskStatus::InProgress,
            subtasks: Vec::new(),
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        }
    }

    /// With the sink wired (UC_GRAPH_SHADOW=on at the assembly point), every
    /// persist_task must also reach the graph writer.
    #[tokio::test]
    async fn persist_task_fans_out_to_graph_shadow_sink() {
        let mut store = TaskStore::new();
        let sink = Arc::new(RecordingGraphSink::default());
        store.set_graph_shadow(sink.clone());

        let task = shadow_test_task("shadow-1");
        store.persist_task(&task);

        // Yield so the fire-and-forget spawn runs (same convention as the
        // EventStore append tests).
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert_eq!(sink.seen.lock().unwrap().as_slice(), ["shadow-1"]);
    }

    /// Default (no sink): persist_task must not touch the graph path at all.
    #[tokio::test]
    async fn persist_task_without_graph_sink_is_unchanged() {
        let store = TaskStore::new();
        let task = shadow_test_task("shadow-2");
        store.persist_task(&task);
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        // No panic, no backend writes — the HashMap/PG path is untouched.
        assert!(store.get_task("shadow-2").is_none());
    }

    // ── T3 (#639): graph-plane verb fan-out at the mutation waypoints ──

    fn yielded() -> impl std::future::Future<Output = ()> {
        // Yield so the fire-and-forget verb spawns run (same convention as
        // the EventStore append + T2 shadow-fanout tests).
        tokio::time::sleep(std::time::Duration::from_millis(50))
    }

    fn wired_store() -> (TaskStore, Arc<RecordingGraphSink>) {
        let mut store = TaskStore::new();
        let sink = Arc::new(RecordingGraphSink::default());
        store.set_graph_shadow(sink.clone());
        (store, sink)
    }

    /// Publish/dispatch mark subtasks `Assigned` through
    /// `update_subtask_status` (both mouths), so the schedule verb rides the
    /// shared mutation — once per fresh dispatch, never on re-marking.
    #[tokio::test]
    async fn graph_schedule_verb_fires_on_assigned_marking() {
        let (mut store, sink) = wired_store();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();

        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        yielded().await;
        assert_eq!(
            sink.take_verbs(),
            vec![format!("schedule {task_id}:{st_id}:-")],
            "Assigned marking fans out exactly one schedule verb"
        );

        // Re-marking the same subtask Assigned is not a new attempt.
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        yielded().await;
        assert!(
            sink.take_verbs().is_empty(),
            "Assigned→Assigned must not re-schedule"
        );

        // Non-Assigned transitions never fire schedule.
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::InProgress);
        yielded().await;
        assert!(sink.take_verbs().is_empty());
    }

    /// The `uc.task.update` terminal-derivation path fans
    /// InProgress→heartbeat, Completed→commit (with the result as
    /// result_ref), Failed/Conflicted→fail — one verb per real transition.
    #[tokio::test]
    async fn graph_verbs_fire_at_update_waypoints() {
        let (mut store, sink) = wired_store();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        sink.take_verbs();

        let mut update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "in_progress".to_string(),
                assigned_worker: Some("w1".to_string()),
                description: None,
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        };
        store.apply_update(&update);
        yielded().await;
        assert_eq!(
            sink.take_verbs(),
            vec![format!("heartbeat {task_id}:{st_id}")]
        );

        // Duplicate report of the same status is not a transition — no verb.
        store.apply_update(&update);
        yielded().await;
        assert!(sink.take_verbs().is_empty());

        update.subtasks[0].status = "completed".to_string();
        update.subtasks[0].result = Some("did it".to_string());
        store.apply_update(&update);
        yielded().await;
        assert_eq!(
            sink.take_verbs(),
            vec![format!("commit {task_id}:{st_id}:did it")],
            "terminal Completed derives commit_once with the result"
        );

        // A brand-new already-failed subtask (late worker result for an
        // unknown node) derives fail on creation.
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "late-fail".to_string(),
                status: "failed".to_string(),
                assigned_worker: None,
                description: Some("late".to_string()),
                depends_on: None,
                result: None,
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        yielded().await;
        assert_eq!(
            sink.take_verbs(),
            vec![format!("fail {task_id}:late-fail:worker_failed")]
        );
    }

    /// T6 (#642): the reaper fail-verb waypoint is gone — the graph fail
    /// rides the sweep itself (inside the sink), and the legacy bridge
    /// (`revert_swept_subtask`) only mirrors the outcome onto the legacy
    /// row: no legacy fail verb, status moved, worker cleared, shadow
    /// persist still flowing.
    #[tokio::test]
    async fn sweep_bridge_replaces_reaper_fail_verb_waypoint() {
        let (mut store, sink) = wired_store();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            t.subtasks[0].assigned_worker = Some(uc_types::WorkerId("worker-1".to_string()));
        }
        assert!(store.revert_swept_subtask(&task_id, &st_id, true));
        yielded().await;
        assert!(
            sink.take_verbs().is_empty(),
            "the bridge must not fan a legacy fail verb — the graph fail rides the sweep"
        );
        let got = store.get_task(&task_id).unwrap();
        assert_eq!(got.subtasks[0].status, uc_types::SubtaskStatus::Pending);
        assert!(got.subtasks[0].assigned_worker.is_none());
        assert!(
            !sink.seen.lock().unwrap().is_empty(),
            "persist_task fan-out keeps running beside the bridge"
        );
    }

    // ── D6 pause-grace timer (T6 #642) ──────────────────────────────

    /// Shared-arc variant of `wired_store` for the timer tests: the spawned
    /// timer needs `Arc<Mutex<TaskStore>>`, the test needs the same store.
    async fn wired_shared_store() -> (
        Arc<Mutex<TaskStore>>,
        Arc<RecordingGraphSink>,
        Arc<std::sync::Mutex<HashMap<String, tokio::task::AbortHandle>>>,
    ) {
        let sink = Arc::new(RecordingGraphSink::default());
        let mut store = TaskStore::new();
        store.set_graph_shadow(sink.clone());
        (
            Arc::new(Mutex::new(store)),
            sink,
            Arc::new(std::sync::Mutex::new(HashMap::new())),
        )
    }

    /// The grace timer fires after the window, drives the graph plane's
    /// fail_running_attempts verb, and bridges each outcome into the legacy
    /// store (InProgress → Pending on re-arm).
    #[tokio::test]
    async fn pause_grace_timer_fires_and_bridges_after_grace() {
        let (store, sink, timers) = wired_shared_store().await;
        let (task_id, st_id) = {
            let mut s = store.lock().await;
            let task = s.submit_task("Test task".to_string(), "p1".to_string());
            let ids = (task.id.0.clone(), task.subtasks[0].id.0.clone());
            if let Some(t) = s.tasks.get_mut(&ids.0) {
                t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            }
            assert!(s.pause_task(&ids.0).is_ok());
            ids
        };
        *sink.fail_running_swept.lock().unwrap() = vec![uc_engine::SweptAttempt {
            graph_id: task_id.clone(),
            node_id: st_id.clone(),
            attempt_id: "g:n:0".to_string(),
            rearmed: true,
        }];
        spawn_pause_grace_timer(
            task_id.clone(),
            std::time::Duration::from_millis(50),
            store.clone(),
            timers.clone(),
            timer_nats(),
        );
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        assert_eq!(
            sink.take_fail_running_calls(),
            vec![(task_id.clone(), "pause_grace_expired".to_string())],
            "the grace hard stop must hit the graph plane with the D6 reason"
        );
        let bridged = {
            let s = store.lock().await;
            s.get_task(&task_id).unwrap().clone()
        };
        assert_eq!(
            bridged.subtasks[0].status,
            uc_types::SubtaskStatus::Pending,
            "re-armed attempt bridges the legacy subtask back to Pending"
        );
        assert!(bridged.subtasks[0].assigned_worker.is_none());
    }

    /// Resuming before the grace lapses leaves the attempts untouched: the
    /// still-paused guard inside the timer observes the resumed task and
    /// no-ops.
    #[tokio::test]
    async fn pause_grace_timer_skips_when_task_resumed_before_fire() {
        let (store, sink, timers) = wired_shared_store().await;
        let task_id = {
            let mut s = store.lock().await;
            let task = s.submit_task("Test task".to_string(), "p1".to_string());
            let id = task.id.0.clone();
            if let Some(t) = s.tasks.get_mut(&id) {
                t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            }
            assert!(s.pause_task(&id).is_ok());
            id
        };
        *sink.fail_running_swept.lock().unwrap() = vec![uc_engine::SweptAttempt {
            graph_id: task_id.clone(),
            node_id: "st-1".to_string(),
            attempt_id: "g:n:0".to_string(),
            rearmed: true,
        }];
        spawn_pause_grace_timer(
            task_id.clone(),
            std::time::Duration::from_millis(100),
            store.clone(),
            timers.clone(),
            timer_nats(),
        );
        assert!({ store.lock().await.resume_task(&task_id).is_ok() });
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        assert!(
            sink.take_fail_running_calls().is_empty(),
            "a resumed task must not be hard-stopped by its own grace timer"
        );
        let after = {
            let s = store.lock().await;
            s.get_task(&task_id).unwrap().clone()
        };
        assert_eq!(
            after.subtasks[0].status,
            uc_types::SubtaskStatus::InProgress
        );
    }

    /// Resume cancels the armed timer: the window lapses with the task
    /// running and the graph plane is never asked to fail anything.
    #[tokio::test]
    async fn resume_cancels_pause_grace_timer() {
        let (store, sink, timers) = wired_shared_store().await;
        let task_id = {
            let mut s = store.lock().await;
            let task = s.submit_task("Test task".to_string(), "p1".to_string());
            let id = task.id.0.clone();
            assert!(s.pause_task(&id).is_ok());
            id
        };
        spawn_pause_grace_timer(
            task_id.clone(),
            std::time::Duration::from_secs(60),
            store.clone(),
            timers.clone(),
            timer_nats(),
        );
        assert!(
            timers.lock().unwrap().contains_key(&task_id),
            "arming registers an abortable timer"
        );
        cancel_pause_grace_timer(&task_id, timers.clone());
        assert!(!timers.lock().unwrap().contains_key(&task_id));
        tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        assert!(
            sink.take_fail_running_calls().is_empty(),
            "a cancelled timer must never fire"
        );
    }

    /// All four verbs fire across the three waypoint families, and the
    /// shadow_persist fan-out keeps running beside them (persist_task
    /// untouched by T3).
    #[tokio::test]
    async fn graph_all_four_verbs_fan_out_at_waypoints() {
        let (mut store, sink) = wired_store();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();

        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "completed".to_string(),
                assigned_worker: Some("w1".to_string()),
                description: None,
                depends_on: None,
                result: Some("ok".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        // T6 (#642): the fail verb driver is a terminal-failure derivation on
        // the update path — the reaper fail-verb waypoint is gone (the graph
        // fail rides the sweep itself now).
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "failed".to_string(),
                assigned_worker: Some("w1".to_string()),
                description: None,
                depends_on: None,
                result: Some("boom".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        yielded().await;

        let verbs = sink.take_verbs();
        let kinds: Vec<&str> = verbs
            .iter()
            .map(|v| v.split(' ').next().unwrap_or("?"))
            .collect();
        assert!(kinds.contains(&"schedule"), "verbs: {verbs:?}");
        assert!(kinds.contains(&"commit"), "verbs: {verbs:?}");
        assert!(kinds.contains(&"fail"), "verbs: {verbs:?}");
        // T18 (#668): this update carries no per-step records, so the commit
        // verb must be the pre-T18 string exactly — additive means
        // bit-for-bit unchanged here, not merely "still parses".
        let commit = verbs
            .iter()
            .find(|v| v.starts_with("commit "))
            .expect("a commit verb");
        assert!(
            !commit.contains(":steps="),
            "an update with no step records must not fabricate the annotation: {commit}"
        );
        // heartbeat already exercised separately; here InProgress was never
        // reported, so assert the three that must exist and that every verb
        // string is well-formed.
        assert!(
            verbs.iter().all(|v| v.starts_with("schedule ")
                || v.starts_with("heartbeat ")
                || v.starts_with("commit ")
                || v.starts_with("fail ")),
            "verbs: {verbs:?}"
        );
        assert!(
            !sink.seen.lock().unwrap().is_empty(),
            "shadow_persist must keep fanning out beside the verbs"
        );
    }

    /// T18 (#668): the per-step records ride the same waypoint as `usage`, all
    /// the way to the sink verb.
    ///
    /// This is the hop a dropped parameter hides in: the commit still succeeds,
    /// the node still goes SUCCEEDED, and the only symptom is a terminal event
    /// that quietly lost the disaggregation of its own usage. Asserting on the
    /// verb string the fake composes from what actually arrived is what makes
    /// that drop visible without a database.
    #[tokio::test]
    async fn graph_commit_verb_carries_the_per_step_records() {
        let (mut store, sink) = wired_store();
        let task = store.submit_task("Test".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();

        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "completed".to_string(),
                assigned_worker: Some("w1".to_string()),
                description: None,
                depends_on: None,
                result: Some("ok".to_string()),
                attempt_id: None,
                usage: Some(uc_types::SubtaskUsage {
                    input_tokens: Some(30),
                    output_tokens: Some(6),
                    source: Some("claude-code".to_string()),
                    ..Default::default()
                }),
                steps: Some(vec![
                    uc_types::StepUsage {
                        step_index: 0,
                        parallel_group: String::new(),
                        usage: Some(uc_types::SubtaskUsage {
                            input_tokens: Some(10),
                            source: Some("grok-build".to_string()),
                            ..Default::default()
                        }),
                        source: Some("grok-build".to_string()),
                    },
                    // Ran, adapter reported nothing — stays silent AND named.
                    uc_types::StepUsage {
                        step_index: 1,
                        parallel_group: String::new(),
                        usage: None,
                        source: Some("codex".to_string()),
                    },
                ]),
                review: None,
            }],
            result: None,
        });
        yielded().await;

        let verbs = sink.take_verbs();
        let commit = verbs
            .iter()
            .find(|v| v.starts_with("commit "))
            .unwrap_or_else(|| panic!("no commit verb: {verbs:?}"));
        assert!(
            commit.contains(":steps=2"),
            "the per-step records must reach the sink — otherwise this \
             parameter was dropped somewhere between the wire and the verb: {commit}"
        );
        // Acceptance 4: the node-level block beside it is unchanged — still the
        // one step's numbers (30+6 = 36), not a sum over the records.
        assert!(
            commit.contains(":usage=36/claude-code"),
            "usage=<total>/<source> still describes the node itself: {commit}"
        );
    }

    /// T18 (#668): the Python publisher's `steps` key deserializes into the
    /// wire struct, and a pre-T18 publisher that omits it still parses.
    ///
    /// Every other test here builds `NatsSubtaskUpdate` in Rust, which proves
    /// the *shape* but never exercises `Deserialize` — a rename or a missing
    /// `#[serde(default)]` would sail straight through and only fail at
    /// runtime, where the failure mode is a silently dropped whole
    /// `uc.task.update`.
    #[test]
    fn nats_subtask_update_parses_python_step_records() {
        let json = r#"{
            "subtask_id": "st-1",
            "status": "Completed",
            "steps": [
                {"step_index": 0, "parallel_group": "",
                 "usage": {"input_tokens": 10, "source": "grok-build"},
                 "source": "grok-build"},
                {"step_index": 1, "parallel_group": "", "usage": null, "source": "codex"}
            ]
        }"#;
        let update: NatsSubtaskUpdate = serde_json::from_str(json).unwrap();
        let steps = update.steps.expect("the `steps` key must parse");
        assert_eq!(steps.len(), 2);
        assert_eq!(steps[0].step_index, 0);
        assert_eq!(steps[0].source.as_deref(), Some("grok-build"));
        assert!(steps[1].usage.is_none());
        assert_eq!(steps[1].source.as_deref(), Some("codex"));

        // A pre-T18 publisher omits the key entirely: absent, not an error.
        let legacy: NatsSubtaskUpdate =
            serde_json::from_str(r#"{"subtask_id": "st-1", "status": "Completed"}"#).unwrap();
        assert!(legacy.steps.is_none());
    }

    /// With no sink wired, every T3 hook site must be structurally inert
    /// (sync test — no runtime, no spawn, no panic): legacy behavior is the
    /// whole surface.
    #[test]
    fn graph_verb_hooks_inert_without_sink() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test".to_string(), "p".to_string());
        let task_id = task.id.0.clone();
        let st_id = task.subtasks[0].id.0.clone();
        store.update_subtask_status(&task_id, &st_id, uc_types::SubtaskStatus::Assigned);
        store.apply_update(&NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            partial: false,
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: st_id.clone(),
                status: "completed".to_string(),
                assigned_worker: None,
                description: None,
                depends_on: None,
                result: Some("r".to_string()),
                attempt_id: None,
                usage: None,
                steps: None,

                review: None,
            }],
            result: None,
        });
        // T6 (#642): the sweep bridge replaces both reaper call sites —
        // structurally inert without a sink (no panic, no verbs), pure
        // legacy-row mirroring.
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            t.subtasks[0].assigned_worker = Some(uc_types::WorkerId("w9".to_string()));
        }
        store.revert_swept_subtask(&task_id, &st_id, true);
        let got = store.get_task(&task_id).unwrap();
        assert_eq!(got.subtasks[0].status, uc_types::SubtaskStatus::Pending);
    }
}
