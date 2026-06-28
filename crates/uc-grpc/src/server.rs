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
    task_status_to_proto,
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

/// NATS subject for task events (Python -> gRPC).
pub const NATS_SUBJECT_TASK_EVENT: &str = "uc.task.event";

/// NATS subject for consumer heartbeats (Python -> gRPC).
pub const NATS_SUBJECT_HEARTBEAT: &str = "uc.heartbeat";

/// NATS subject for subtask execution dispatch (Rust -> Worker queue group).
pub const NATS_SUBJECT_SUBTASK_EXECUTE: &str = "uc.subtask.execute";

/// Payload for `uc.task.submit` messages.
///
/// Published by gRPC server when a task is submitted. The Python NATS
/// consumer subscribes to this subject and calls Orchestrator.submit_task().
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskSubmit {
    pub task_id: String,
    pub description: String,
    pub project_id: String,
}

/// Payload for `uc.task.update` messages.
///
/// Published by Python Orchestrator when a task or its subtasks change status.
/// The gRPC server subscribes to this subject and updates the in-memory TaskStore.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskUpdate {
    /// Deduplication key for at-least-once NATS delivery.
    /// Format: `{task_id}:{event_type}:{subtask_id}:{timestamp_ms}`
    #[serde(default)]
    pub message_id: Option<String>,
    pub task_id: String,
    pub status: String,
    pub subtasks: Vec<NatsSubtaskUpdate>,
    #[serde(default)]
    pub result: Option<String>,
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
    pub task_id: String,
    pub subtask_id: String,
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
    /// Capabilities required by this subtask (e.g., "rust", "python", "docker").
    /// Worker must possess ALL listed capabilities to accept this subtask.
    #[serde(default)]
    pub required_capabilities: Vec<String>,
}

fn default_timeout() -> u64 {
    600
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
/// via `apply_update()`. When NATS is unavailable, tasks are decomposed
/// locally using a newline-split heuristic.
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
    /// ponytail: stored for future wiring; sync methods still use HashMap
    #[allow(dead_code)]
    task_backend: Option<Arc<dyn uc_engine::TaskStoreBackend>>,
    /// Last heartbeat timestamp from Python NATS consumer.
    last_heartbeat: Option<chrono::DateTime<chrono::Utc>>,
    /// Per-Worker heartbeat timestamps (worker_id -> last seen).
    /// Used for distributed Worker failure detection.
    worker_heartbeats: HashMap<String, chrono::DateTime<chrono::Utc>>,
    /// Deduplication map for NATS at-least-once delivery.
    /// Keys are message_id strings; values are insertion timestamps.
    /// Entries older than 5 minutes are purged on each check.
    seen_messages: HashMap<String, Instant>,
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
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            seen_messages: HashMap::new(),
        }
    }

    /// Create with a specific EventStore backend.
    pub fn with_event_store(event_store: Arc<dyn uc_engine::EventStore>) -> Self {
        Self {
            tasks: HashMap::new(),
            events: Vec::new(),
            event_store,
            task_backend: None,
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            seen_messages: HashMap::new(),
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
            last_heartbeat: None,
            worker_heartbeats: HashMap::new(),
            seen_messages: HashMap::new(),
        }
    }

    /// Dedup TTL: messages older than this are considered expired.
    const DEDUP_TTL: std::time::Duration = std::time::Duration::from_secs(300); // 5 minutes

    /// Maximum seen_messages entries before triggering a purge.
    const DEDUP_MAX_ENTRIES: usize = 10_000;

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
    fn record_event_with_subject(&mut self, event: uc_engine::AgentEventType, subject: &str) {
        // Inline log (legacy, for tests and immediate reads)
        self.events.push(event.clone());
        // EventStore (unified, persistent source of truth)
        // ponytail: spawn is fire-and-forget; if no runtime, skip (tests)
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            let es = self.event_store.clone();
            let subj = subject.to_string();
            handle.spawn(async move {
                let _ = es.append(&subj, &event).await;
            });
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
            dispatch_retry_count: 0,
            required_capabilities: Vec::new(),
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
    ) -> uc_types::Task {
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
        self.record_event_with_subject(
            uc_engine::AgentEventType::TaskCreated {
                task_id: task_id.clone(),
                description,
            },
            &format!("task.{}", task_id.0),
        );

        let task_id_str = task.id.0.clone();
        self.tasks.insert(task_id_str, task.clone());
        task
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
                Ok(task.clone())
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
                Ok(task.clone())
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
                        st.status = uc_types::SubtaskStatus::Failed;
                    }
                }
                Ok(task.clone())
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
        if !self.tasks.contains_key(task_id) && !description.is_empty() {
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

        // Update status
        if let Ok(parsed) = proto_status_to_task_status(status) {
            task.status = parsed;
        }
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
    /// If the task does not exist, logs a warning and does nothing (graceful
    /// handling of stale or out-of-order messages).
    pub fn apply_update(&mut self, update: &NatsTaskUpdate) {
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
        if let Some(status) = task_status_from_str(&update.status) {
            task.status = status;
        } else {
            tracing::warn!(
                task_id = %update.task_id,
                status = %update.status,
                "Unknown task status in NATS update, ignoring status field"
            );
        }

        // Update result if provided
        if update.result.is_some() {
            // Task-level result is not directly stored in the current Task struct,
            // but we update the timestamp to reflect the change.
        }

        // Update subtasks — full upsert
        for subtask_update in &update.subtasks {
            if let Some(subtask) = task
                .subtasks
                .iter_mut()
                .find(|st| st.id.0 == subtask_update.subtask_id)
            {
                // Existing subtask — update all provided fields
                if let Some(status) = subtask_status_from_str(&subtask_update.status) {
                    subtask.status = status;
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
                    // ponytail: derive success from subtask status, not hardcoded true
                    let success = subtask_update.status != "failed";
                    subtask.result = Some(uc_types::SubtaskResult {
                        subtask_id: subtask.id.clone(),
                        worker_id: subtask.assigned_worker.clone().unwrap_or_default(),
                        modified_files: Vec::new(),
                        summary: result_str.clone(),
                        success,
                        completed_at: chrono::Utc::now(),
                        result: Some(result_str.clone()),
                    });
                }
            } else {
                // New subtask from Python Orchestrator — create with all provided fields
                let new_subtask = uc_types::Subtask {
                    id: uc_types::TaskId(subtask_update.subtask_id.clone()),
                    parent_id: task.id.clone(),
                    description: subtask_update.description.clone().unwrap_or_default(),
                    status: subtask_status_from_str(&subtask_update.status)
                        .unwrap_or(uc_types::SubtaskStatus::Pending),
                    assigned_worker: subtask_update
                        .assigned_worker
                        .as_ref()
                        .map(|w| uc_types::WorkerId(w.clone())),
                    depends_on: subtask_update
                        .depends_on
                        .as_ref()
                        .map(|deps| deps.iter().map(|d| uc_types::TaskId(d.clone())).collect())
                        .unwrap_or_default(),
                    file_constraints: Vec::new(),
                    expected_output: String::new(),
                    result: subtask_update
                        .result
                        .as_ref()
                        .map(|r| uc_types::SubtaskResult {
                            subtask_id: uc_types::TaskId(subtask_update.subtask_id.clone()),
                            worker_id: subtask_update
                                .assigned_worker
                                .as_ref()
                                .map(|w| uc_types::WorkerId(w.clone()))
                                .unwrap_or_default(),
                            modified_files: Vec::new(),
                            summary: r.clone(),
                            success: true,
                            completed_at: chrono::Utc::now(),
                            result: Some(r.clone()),
                        }),
                    dispatch_mode: uc_types::DispatchMode::default(),
                    dispatch_retry_count: 0,
                    required_capabilities: Vec::new(),
                };
                task.subtasks.push(new_subtask);
            }
        }

        task.updated_at = chrono::Utc::now();
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
    pub fn update_worker_heartbeat(&mut self, worker_id: &str) {
        self.worker_heartbeats
            .insert(worker_id.to_string(), chrono::Utc::now());
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

    /// Reassign subtasks assigned to stale workers back to Pending.
    /// Returns (task_ids_affected, subtask_ids_reassigned).
    pub fn reassign_stale_subtasks(
        &mut self,
        stale_worker_ids: &[String],
    ) -> (Vec<String>, Vec<String>) {
        let mut affected_tasks = Vec::new();
        let mut reassigned = Vec::new();
        for task in self.tasks.values_mut() {
            for st in &mut task.subtasks {
                if matches!(
                    st.status,
                    uc_types::SubtaskStatus::InProgress | uc_types::SubtaskStatus::Assigned
                ) {
                    if let Some(ref w) = st.assigned_worker {
                        if stale_worker_ids.contains(&w.0) {
                            st.status = uc_types::SubtaskStatus::Pending;
                            st.assigned_worker = None;
                            reassigned.push(st.id.0.clone());
                            if !affected_tasks.contains(&task.id.0) {
                                affected_tasks.push(task.id.0.clone());
                            }
                        }
                    }
                }
            }
        }
        (affected_tasks, reassigned)
    }

    /// Get the last heartbeat timestamp.
    pub fn last_heartbeat(&self) -> Option<chrono::DateTime<chrono::Utc>> {
        self.last_heartbeat
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
        if let Some(task) = self.tasks.get_mut(task_id) {
            if let Some(st) = task.subtasks.iter_mut().find(|s| s.id.0 == subtask_id) {
                st.status = new_status;
                task.updated_at = chrono::Utc::now();
            }
        }
    }

    /// Increment a subtask's dispatch_retry_count within a task.
    /// Returns the new retry count, or None if task/subtask not found.
    pub fn increment_dispatch_retry(&mut self, task_id: &str, subtask_id: &str) -> Option<u32> {
        if let Some(task) = self.tasks.get_mut(task_id) {
            if let Some(st) = task.subtasks.iter_mut().find(|s| s.id.0 == subtask_id) {
                st.dispatch_retry_count += 1;
                task.updated_at = chrono::Utc::now();
                return Some(st.dispatch_retry_count);
            }
        }
        None
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
        for (id, task) in &mut self.tasks {
            match task.status {
                uc_types::TaskStatus::InProgress | uc_types::TaskStatus::Planning => {
                    task.status = uc_types::TaskStatus::Failed;
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
    /// Worker registry — source of truth for WorkerService and capability-aware dispatch.
    worker_registry: Arc<RwLock<WorkerRegistry>>,
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

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Create a new gRPC server wrapping the given engine, without NATS.
    ///
    /// Falls back to local (newline-split) decomposition if NATS is
    /// unavailable.
    pub fn new(engine: E) -> Self {
        let (event_tx, _) = broadcast::channel(256);

        let task_store = Arc::new(Mutex::new(TaskStore::new()));
        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));

        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store,
                worker_registry,
                #[cfg(feature = "messaging")]
                nats_client: None,
                event_tx,
            }),
        }
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
            event_store,
        )));
        let worker_registry = Arc::new(RwLock::new(WorkerRegistry::new()));

        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store,
                worker_registry,
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
        let nats_client = match async_nats::connect(nats_url).await {
            Ok(client) => {
                tracing::info!(nats_url = %nats_url, "Connected to NATS for TaskService");
                Some(client)
            }
            Err(e) => {
                tracing::warn!(
                    nats_url = %nats_url,
                    error = %e,
                    "NATS unavailable, TaskService will use local decomposition"
                );
                None
            }
        };

        let task_store = Arc::new(Mutex::new(TaskStore::with_backend(
            task_backend,
            event_store,
        )));

        let (event_tx, _) = broadcast::channel(256);

        let inner = Arc::new(GrpcServerInner {
            engine,
            task_store: task_store.clone(),
            worker_registry: Arc::new(RwLock::new(WorkerRegistry::new())),
            nats_client: nats_client.clone(),
            event_tx: event_tx.clone(),
        });

        // Spawn background subscriber and heartbeat monitor if NATS is connected
        if let Some(client) = nats_client {
            spawn_nats_subscriber(client.clone(), task_store.clone(), event_tx);
            spawn_heartbeat_monitor(client, task_store.clone(), heartbeat_timeout);
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
        let nats_client = match async_nats::connect(nats_url).await {
            Ok(client) => {
                tracing::info!(nats_url = %nats_url, "Connected to NATS for TaskService");
                Some(client)
            }
            Err(e) => {
                tracing::warn!(
                    nats_url = %nats_url,
                    error = %e,
                    "NATS unavailable, TaskService will use local decomposition"
                );
                None
            }
        };

        let task_store = Arc::new(Mutex::new(TaskStore::new()));

        let (event_tx, _) = broadcast::channel(256);

        let inner = Arc::new(GrpcServerInner {
            engine,
            task_store: task_store.clone(),
            worker_registry: Arc::new(RwLock::new(WorkerRegistry::new())),
            nats_client: nats_client.clone(),
            event_tx: event_tx.clone(),
        });

        // Spawn background subscriber and heartbeat monitor if NATS is connected
        if let Some(client) = nats_client {
            spawn_nats_subscriber(client.clone(), task_store.clone(), event_tx);
            spawn_heartbeat_monitor(client, task_store.clone(), heartbeat_timeout);
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
        (engine_service, task_service, dashboard_service, worker_service)
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
        use std::time::{SystemTime, UNIX_EPOCH};
        if let Some(nats_client) = &self.inner.nats_client {
            let ts_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            let message_id = format!("{}:{}::{}", task_id, event_type, ts_ms);
            let event = NatsTaskEvent {
                v: default_event_version(),
                message_id: Some(message_id.clone()),
                r#type: event_type.to_string(),
                task_id: task_id.to_string(),
                subtask_id: None,
                data: serde_json::Map::new(),
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
                            "Failed to publish NATS task status event"
                        );
                    } else {
                        // Register message_id in dedup map so the NATS
                        // subscriber skips the echo of our own message.
                        let mut store = self.inner.task_store.lock().await;
                        store.check_and_record_message_id(&Some(message_id));
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        error = %e,
                        "Failed to serialize NATS task status event"
                    );
                }
            }
        }
    }

    #[cfg(not(feature = "messaging"))]
    async fn publish_task_status_event(&self, _task_id: &str, _event_type: &str) {}

    /// Dispatch ready subtasks for a task to the NATS `uc.subtask.execute` subject.
    ///
    /// Called after a task is decomposed (subtasks populated) and after each
    /// `uc.task.update` that may have completed dependencies.
    ///
    /// Marks dispatched subtasks as `Assigned` in TaskStore so they are not
    /// re-dispatched on the next call.
    #[cfg(feature = "messaging")]
    pub async fn publish_ready_subtasks(&self, task_id: &str) {
        let ready = {
            let mut store = self.inner.task_store.lock().await;
            let subtasks = store.get_ready_subtasks(task_id);

            // Check WorkerRegistry for capability-aware dispatch:
            // Only mark as Assigned if a matching worker exists (or no capabilities required).
            let registry = self.inner.worker_registry.read().await;
            let mut dispatchable = Vec::new();
            for st in &subtasks {
                if !st.required_capabilities.is_empty() {
                    let matching = registry.workers_with_capabilities(&st.required_capabilities);
                    if matching.is_empty() {
                        tracing::info!(
                            subtask_id = %st.id.0,
                            required_capabilities = ?st.required_capabilities,
                            "No worker with matching capabilities, keeping subtask Pending"
                        );
                        continue; // skip — don't mark as Assigned
                    }
                }
                store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Assigned);
                dispatchable.push(st.clone());
            }
            drop(registry);
            dispatchable
        };

        if ready.is_empty() {
            return;
        }

        if let Some(nats_client) = &self.inner.nats_client {
            for st in ready {
                // Local mode: skip NATS publish entirely
                if st.dispatch_mode == uc_types::DispatchMode::Local {
                    tracing::info!(
                        subtask_id = %st.id.0,
                        "Subtask dispatch_mode=Local, skipping NATS publish"
                    );
                    continue;
                }

                let execute = NatsSubtaskExecute {
                    message_id: Some(format!(
                        "{}:execute:{}:{}",
                        task_id,
                        st.id.0,
                        std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap_or_default()
                            .as_millis()
                    )),
                    task_id: task_id.to_string(),
                    subtask_id: st.id.0.clone(),
                    description: st.description.clone(),
                    expected_output: String::new(),
                    file_constraints: Vec::new(),
                    timeout_seconds: 600,
                    retry_count: st.dispatch_retry_count,
                    dispatch_mode: st.dispatch_mode.clone(),
                    required_capabilities: st.required_capabilities.clone(),
                };
                match serde_json::to_vec(&execute) {
                    Ok(bytes) => {
                        if let Err(e) = nats_client
                            .publish(NATS_SUBJECT_SUBTASK_EXECUTE.to_string(), bytes.into())
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

/// Spawn a background task that subscribes to `uc.task.update`,
/// `uc.task.event`, and `uc.heartbeat`, updating the TaskStore accordingly.
#[cfg(feature = "messaging")]
fn spawn_nats_subscriber(
    nats_client: async_nats::Client,
    task_store: Arc<Mutex<TaskStore>>,
    event_tx: broadcast::Sender<TaskEvent>,
) {
    use futures::StreamExt;

    tokio::spawn(async move {
        // Subscribe to task updates
        let mut update_sub = match nats_client.subscribe(NATS_SUBJECT_TASK_UPDATE).await {
            Ok(sub) => sub,
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "Failed to subscribe to NATS task updates, subscriber not running"
                );
                return;
            }
        };

        // Subscribe to task events
        let mut event_sub = match nats_client.subscribe(NATS_SUBJECT_TASK_EVENT).await {
            Ok(sub) => sub,
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "Failed to subscribe to NATS task events, event subscriber not running"
                );
                return;
            }
        };

        // Subscribe to heartbeats
        let mut heartbeat_sub = match nats_client.subscribe(NATS_SUBJECT_HEARTBEAT).await {
            Ok(sub) => sub,
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "Failed to subscribe to NATS heartbeats, heartbeat monitoring not active"
                );
                return;
            }
        };

        tracing::info!("NATS subscriber started for TaskService");

        loop {
            tokio::select! {
                Some(message) = update_sub.next() => {
                    match serde_json::from_slice::<NatsTaskUpdate>(&message.payload) {
                        Ok(update) => {
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
                            let event_count_before;
                            {
                                let mut store = task_store.lock().await;
                                event_count_before = store.events.len();
                                store.apply_update(&update);
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
                                        if let Some(subtask) = task.subtasks.iter().find(|st| st.id.0 == subtask_update.subtask_id) {
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

                            // Record the collected events
                            {
                                let mut store = task_store.lock().await;
                                for e in events_to_record {
                                    store.record_event(e);
                                }
                            }

                            // Broadcast newly recorded events to all WatchTask streams
                            let new_events: Vec<TaskEvent> = {
                                let store = task_store.lock().await;
                                store.events[event_count_before..]
                                    .iter()
                                    .cloned()
                                    .map(|e| e.into())
                                    .collect()
                            };
                            for event in new_events {
                                let _ = event_tx.send(event);
                            }

                            // Dispatch ready subtasks for this task
                            dispatch_ready_subtasks(
                                &task_store,
                                &nats_client,
                                &update.task_id,
                            ).await;
                        }
                        Err(e) => {
                            tracing::warn!(
                                error = %e,
                                "Failed to parse NATS task update message"
                            );
                        }
                    }
                }
                Some(message) = event_sub.next() => {
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
                Some(message) = heartbeat_sub.next() => {
                    let mut store = task_store.lock().await;
                    store.update_last_heartbeat();
                    // Also track per-worker heartbeat for failover detection.
                    if let Ok(hb) = serde_json::from_slice::<NatsHeartbeat>(&message.payload) {
                        store.update_worker_heartbeat(&hb.consumer_id);
                    }
                }
                else => {
                    tracing::warn!("NATS subscription ended, subscriber exiting");
                    break;
                }
            }
        }
    });
}

/// Spawn a background task that periodically checks for heartbeat timeouts
/// and marks stale tasks as Failed.
#[cfg(feature = "messaging")]
fn spawn_heartbeat_monitor(
    nats_client: async_nats::Client,
    task_store: Arc<Mutex<TaskStore>>,
    heartbeat_timeout: std::time::Duration,
) {
    tokio::spawn(async move {
        // Check every 30 seconds
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));

        loop {
            interval.tick().await;

            let mut store = task_store.lock().await;
            let failed = store.mark_stale_tasks_failed(heartbeat_timeout);
            if !failed.is_empty() {
                tracing::warn!(
                    task_ids = ?failed,
                    "Marked tasks as Failed due to heartbeat timeout"
                );
            }

            // Worker-level failover: detect stale workers and reassign their subtasks.
            let stale_workers = store.mark_stale_workers(heartbeat_timeout);
            if !stale_workers.is_empty() {
                let (affected_tasks, reassigned) = store.reassign_stale_subtasks(&stale_workers);
                tracing::warn!(
                    worker_ids = ?stale_workers,
                    tasks_affected = affected_tasks.len(),
                    subtasks_reassigned = reassigned.len(),
                    "Reassigned subtasks from stale workers back to Pending"
                );
                // Drop the lock before dispatching (dispatch acquires it again)
                drop(store);
                // Re-dispatch reassigned subtasks so live workers pick them up
                for task_id in &affected_tasks {
                    dispatch_ready_subtasks(&task_store, &nats_client, task_id).await;
                }
            }
        }
    });
}

/// Dispatch ready subtasks for a task by publishing them to `uc.subtask.execute`.
///
/// Called by the NATS subscriber after processing a `uc.task.update` —
/// completing a subtask may unblock dependents.
#[cfg(feature = "messaging")]
async fn dispatch_ready_subtasks(
    task_store: &Arc<Mutex<TaskStore>>,
    nats_client: &async_nats::Client,
    task_id: &str,
) {
    let ready = {
        let mut store = task_store.lock().await;
        let subtasks = store.get_ready_subtasks(task_id);
        for st in &subtasks {
            store.update_subtask_status(task_id, &st.id.0, uc_types::SubtaskStatus::Assigned);
        }
        subtasks
    };

    for st in ready {
        // Local mode: skip NATS publish entirely
        if st.dispatch_mode == uc_types::DispatchMode::Local {
            tracing::info!(
                subtask_id = %st.id.0,
                "Subtask dispatch_mode=Local, skipping NATS publish"
            );
            continue;
        }

        let execute = NatsSubtaskExecute {
            message_id: Some(format!(
                "{}:execute:{}:{}",
                task_id,
                st.id.0,
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis()
            )),
            task_id: task_id.to_string(),
            subtask_id: st.id.0.clone(),
            description: st.description.clone(),
            expected_output: String::new(),
            file_constraints: Vec::new(),
            timeout_seconds: 600,
            retry_count: st.dispatch_retry_count,
            dispatch_mode: st.dispatch_mode.clone(),
            required_capabilities: st.required_capabilities.clone(),
        };
        match serde_json::to_vec(&execute) {
            Ok(bytes) => {
                if let Err(e) = nats_client
                    .publish(NATS_SUBJECT_SUBTASK_EXECUTE.to_string(), bytes.into())
                    .await
                {
                    tracing::warn!(
                        error = %e,
                        subtask_id = %st.id.0,
                        dispatch_mode = ?st.dispatch_mode,
                        "Failed to publish subtask execute"
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
        };
        let result = self
            .inner
            .engine
            .write_memory(write_req)
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
        _request: Request<ListReposRequest>,
    ) -> Result<Response<ListReposResponse>, Status> {
        let repos = self.inner.engine.list_repos().await.map_err(to_status)?;
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
                    let event_count_before = store.events.len();
                    let task =
                        store.submit_task_pending(req.description.clone(), req.project_id.clone());

                    let payload = NatsTaskSubmit {
                        task_id: task.id.0.clone(),
                        description: req.description.clone(),
                        project_id: req.project_id.clone(),
                    };
                    let events: Vec<TaskEvent> = store.events[event_count_before..]
                        .iter()
                        .cloned()
                        .map(|e| e.into())
                        .collect();
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

        // No NATS or NATS publish failed — no Rust-side fallback
        // All decomposition must go through Python Orchestrator via NATS.
        Ok(Response::new(SubmitTaskResponse {
            success: false,
            task_id: String::new(),
            status: "failed".to_string(),
            subtask_count: 0,
            subtasks: Vec::new(),
            error: Some("No NATS connection available. Connect NATS and start a Python worker to enable task submission.".to_string()),
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
            let last_replayed_idx = {
                let s = task_store.lock().await;
                if task_id.is_empty() {
                    // Skip replay for "watch all" — TUI doesn't need history
                    None
                } else {
                    let events = s.read_events_from(0);
                    let mut last_idx: Option<u64> = None;
                    for (i, event) in events.iter().enumerate() {
                        let proto_event: TaskEvent = event.clone().into();
                        if proto_event.task_id != task_id {
                            continue;
                        }
                        yield Ok(proto_event);
                        last_idx = Some(i as u64);
                    }
                    last_idx
                }
            };

            // Phase 2: listen for new events via broadcast.
            // The receiver was created before Phase 1, so there is no gap.
            // Skip events that were already replayed in Phase 1 (identified by
            // "event_idx" in data map, set by the task store on each event).
            let mut rx = event_rx;
            // ponytail: dedup by counting — skip first N events that overlap
            // with the replay. The broadcast buffer holds events from the
            // subscribe point, so Phase 2 may re-deliver events we just replayed.
            loop {
                match rx.recv().await {
                    Ok(proto_event) => {
                        if !task_id.is_empty() && proto_event.task_id != task_id {
                            continue;
                        }
                        // Dedup: skip events that were already yielded in Phase 1.
                        // We check the event_idx field if present, otherwise
                        // skip the first N events matching the replay count.
                        if let Some(idx_str) = proto_event.data.get("event_idx") {
                            if let Ok(idx) = idx_str.parse::<u64>() {
                                if let Some(last) = last_replayed_idx {
                                    if idx <= last {
                                        continue; // already replayed
                                    }
                                }
                            }
                        }
                        yield Ok(proto_event);
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!(
                            skipped = n,
                            "WatchTask broadcast receiver lagged, some events dropped"
                        );
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

    async fn cancel_task(
        &self,
        request: Request<CancelTaskRequest>,
    ) -> Result<Response<CancelTaskResponse>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id.clone();
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
                    file_constraints: Vec::new(),
                    expected_output: String::new(),
                    result: None, // ponytail: SubtaskResult is complex struct; result tracked via SubtaskCompleted events
                    dispatch_mode: uc_types::DispatchMode::default(),
                    dispatch_retry_count: 0,
                    required_capabilities: Vec::new(),
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
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {}

#[cfg(test)]
mod tests {
    use super::*;

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
        let task =
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

    // decompose_task tests removed — Rust-side decomposition no longer exists;
    // all decomposition goes through Python Orchestrator via NATS/bridge.

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
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsTaskSubmit = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.task_id, "abc-123");
        assert_eq!(parsed.description, "Fix the login bug");
        assert_eq!(parsed.project_id, "proj-1");
    }

    #[test]
    fn nats_task_update_serialization() {
        let msg = NatsTaskUpdate {
            message_id: Some("abc-123:update::1700000000".to_string()),
            task_id: "abc-123".to_string(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
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
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsHeartbeat = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.consumer_id, "consumer-1");
        assert_eq!(parsed.timestamp, "2026-06-16T12:00:00Z");
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
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-new-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: None,
                depends_on: None,
                result: None,
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
            subtasks: vec![],
            result: None,
        };

        // Should not panic, just log a warning
        store.apply_update(&update);
        assert!(store.get_task("nonexistent").is_none());
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
            subtasks: vec![],
            result: None,
        };

        store.apply_update(&update);

        // Status should remain unchanged (unknown status is ignored)
        let updated = store.get_task(&task_id).unwrap();
        assert_eq!(updated.status, uc_types::TaskStatus::InProgress);
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
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "Completed".to_string(),
                assigned_worker: None,
                description: None,
                depends_on: None,
                result: Some("Done".to_string()),
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
    fn task_store_record_event() {
        let mut store = TaskStore::new();
        let initial_count = store.event_count();

        store.record_event(uc_engine::AgentEventType::TaskCreated {
            task_id: uc_types::TaskId::new(),
            description: "Extra event".to_string(),
        });

        assert_eq!(store.event_count(), initial_count + 1);
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
                dispatch_retry_count: 0,
                required_capabilities: Vec::new(),
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
                dispatch_retry_count: 0,
                required_capabilities: Vec::new(),
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

        store.update_worker_heartbeat("worker-1");
        store.update_worker_heartbeat("worker-2");

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

    #[test]
    fn reassign_stale_subtasks_resets_to_pending() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        let subtask_id = task.subtasks[0].id.0.clone();

        // Assign subtask to worker-1
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            t.subtasks[0].assigned_worker = Some(uc_types::WorkerId("worker-1".to_string()));
        }

        let (affected, reassigned) = store.reassign_stale_subtasks(&["worker-1".to_string()]);
        assert_eq!(affected, vec![task_id]);
        assert_eq!(reassigned, vec![subtask_id]);

        // Verify subtask is back to Pending with no assigned worker
        let task = store.get_task(&affected[0]).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::Pending);
        assert!(task.subtasks[0].assigned_worker.is_none());
    }

    #[test]
    fn reassign_skips_non_stale_workers() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Assign to worker-2 (not stale)
        {
            let t = store.tasks.get_mut(&task_id).unwrap();
            t.subtasks[0].status = uc_types::SubtaskStatus::InProgress;
            t.subtasks[0].assigned_worker = Some(uc_types::WorkerId("worker-2".to_string()));
        }

        // Only worker-1 is stale
        let (affected, reassigned) = store.reassign_stale_subtasks(&["worker-1".to_string()]);
        assert!(affected.is_empty());
        assert!(reassigned.is_empty());

        // Subtask still assigned to worker-2
        let task = store.get_task(&task_id).unwrap();
        assert_eq!(task.subtasks[0].status, uc_types::SubtaskStatus::InProgress);
        assert_eq!(
            task.subtasks[0].assigned_worker,
            Some(uc_types::WorkerId("worker-2".to_string()))
        );
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
                subtasks: vec![NatsSubtaskUpdate {
                    subtask_id: subtask_id.clone(),
                    status: "Completed".to_string(),
                    assigned_worker: None,
                    description: None,
                    depends_on: None,
                    result: None,
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
        let event_count_before;
        {
            let mut store = task_store.lock().await;
            event_count_before = store.events.len();
            let update = NatsTaskUpdate {
                message_id: None,
                task_id: task_id.clone(),
                status: "InProgress".to_string(),
                subtasks: vec![NatsSubtaskUpdate {
                    subtask_id: subtask_id.clone(),
                    status: "Assigned".to_string(),
                    assigned_worker: Some("worker-1".to_string()),
                    description: None,
                    depends_on: None,
                    result: None,
                }],
                result: None,
            };
            store.apply_update(&update);
        }

        // Record subtask event (mirrors subscriber logic)
        // Clone needed data first to avoid borrow conflict
        {
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

            if let Some((tid, sid, wid)) = event_data {
                store.record_event(uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: tid,
                    subtask_id: sid,
                    worker_id: wid,
                });
            }
        }

        // Broadcast new events
        let new_events: Vec<TaskEvent> = {
            let store = task_store.lock().await;
            store.events[event_count_before..]
                .iter()
                .cloned()
                .map(|e| e.into())
                .collect()
        };
        for event in new_events {
            let _ = event_tx.send(event);
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
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "InProgress".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                description: Some("Updated description".to_string()),
                depends_on: Some(vec!["other-st".to_string()]),
                result: Some("Work done".to_string()),
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
        let task = store.submit_task_pending("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-new".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-2".to_string()),
                description: Some("New subtask from Python".to_string()),
                depends_on: Some(vec!["dep-1".to_string(), "dep-2".to_string()]),
                result: None,
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
    fn apply_update_backward_compat_old_format() {
        // Verify that old-format updates (without description/depends_on)
        // still work — description defaults to empty, depends_on defaults to empty vec.
        let mut store = TaskStore::new();
        let task = store.submit_task_pending("Test task".to_string(), "proj".to_string());
        let task_id = task.id.0.clone();

        let update = NatsTaskUpdate {
            message_id: None,
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-old".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: None,
                description: None,
                depends_on: None,
                result: None,
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
}
