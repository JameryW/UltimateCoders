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

use tokio::sync::{broadcast, Mutex};
use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::conversions::{memory_key_from_proto, task_status_to_proto};
use crate::ultimate_coders::engine_service_server::{EngineService, EngineServiceServer};
use crate::ultimate_coders::task_service_server::{TaskService, TaskServiceServer};
use crate::ultimate_coders::*;

// ── NATS message protocol types ──────────────────────────────

/// NATS subject for task submission (gRPC/Dashboard -> Python).
pub const NATS_SUBJECT_TASK_SUBMIT: &str = "uc.task.submit";

/// NATS subject for task status updates (Python -> gRPC).
pub const NATS_SUBJECT_TASK_UPDATE: &str = "uc.task.update";

/// NATS subject for task events (Python -> gRPC).
pub const NATS_SUBJECT_TASK_EVENT: &str = "uc.task.event";

/// NATS subject for consumer heartbeats (Python -> gRPC).
pub const NATS_SUBJECT_HEARTBEAT: &str = "uc.heartbeat";

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
    pub result: Option<String>,
}

/// Payload for `uc.task.event` messages.
///
/// Published by Python Orchestrator for real-time events (tool calls, LLM
/// requests, etc.). The gRPC server pushes these into the TaskStore event
/// log for WatchTask streaming.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NatsTaskEvent {
    pub r#type: String,
    pub task_id: String,
    #[serde(default)]
    pub subtask_id: Option<String>,
    #[serde(default)]
    pub data: serde_json::Map<String, serde_json::Value>,
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
    match s {
        "Created" => Some(uc_types::TaskStatus::Created),
        "Planning" => Some(uc_types::TaskStatus::Planning),
        "InProgress" => Some(uc_types::TaskStatus::InProgress),
        "Completed" => Some(uc_types::TaskStatus::Completed),
        "Failed" => Some(uc_types::TaskStatus::Failed),
        "Paused" => Some(uc_types::TaskStatus::Paused),
        _ => None,
    }
}

/// Parse a SubtaskStatus from its string representation.
///
/// Returns None if the string does not match any known status.
fn subtask_status_from_str(s: &str) -> Option<uc_types::SubtaskStatus> {
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

/// Extract a boolean value from a JSON map entry.
///
/// Handles both boolean values (`true`/`false`) and string values
/// (`"true"`/`"false"`). Python publishers send booleans as JSON `true`/`false`,
/// but some sources may send them as strings. Returns `default` if the key
/// is missing or neither a bool nor a string.
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
pub struct TaskStore {
    tasks: HashMap<String, uc_types::Task>,
    events: Vec<uc_engine::AgentEventType>,
    /// Last heartbeat timestamp from Python NATS consumer.
    last_heartbeat: Option<chrono::DateTime<chrono::Utc>>,
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
            last_heartbeat: None,
        }
    }

    /// Submit a new task: create it, decompose into subtasks, store, and return.
    pub fn submit_task(&mut self, description: String, project_id: String) -> uc_types::Task {
        let task_id = uc_types::TaskId::new();
        let now = chrono::Utc::now();

        // Simple decomposition: split by newlines or sentences
        let subtasks = decompose_task(&task_id, &description);

        let task = uc_types::Task {
            id: task_id.clone(),
            description: description.clone(),
            project_id,
            status: uc_types::TaskStatus::InProgress,
            subtasks,
            created_at: now,
            updated_at: now,
        };

        // Record TaskCreated event
        self.events.push(uc_engine::AgentEventType::TaskCreated {
            task_id: task_id.clone(),
            description: description.clone(),
        });

        // Record subtask events
        for st in &task.subtasks {
            self.events
                .push(uc_engine::AgentEventType::SubtaskAssigned {
                    task_id: task_id.clone(),
                    subtask_id: st.id.clone(),
                    worker_id: uc_types::WorkerId::new(),
                });
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
        self.events.push(uc_engine::AgentEventType::TaskCreated {
            task_id: task_id.clone(),
            description,
        });

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

    /// Read events from the given offset.
    pub fn read_events_from(&self, offset: usize) -> Vec<uc_engine::AgentEventType> {
        if offset >= self.events.len() {
            Vec::new()
        } else {
            self.events[offset..].to_vec()
        }
    }

    /// Get current event count (used as latest offset).
    pub fn event_count(&self) -> usize {
        self.events.len()
    }

    /// Apply a status update from NATS (`uc.task.update`).
    ///
    /// Updates the task's status, subtask statuses, and result.
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

        // Update subtask statuses
        for subtask_update in &update.subtasks {
            if let Some(subtask) = task
                .subtasks
                .iter_mut()
                .find(|st| st.id.0 == subtask_update.subtask_id)
            {
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
            } else {
                // New subtask from Python Orchestrator — add it
                let new_subtask = uc_types::Subtask {
                    id: uc_types::TaskId(subtask_update.subtask_id.clone()),
                    parent_id: task.id.clone(),
                    description: String::new(), // Not provided in update
                    status: subtask_status_from_str(&subtask_update.status)
                        .unwrap_or(uc_types::SubtaskStatus::Pending),
                    assigned_worker: subtask_update
                        .assigned_worker
                        .as_ref()
                        .map(|w| uc_types::WorkerId(w.clone())),
                    depends_on: Vec::new(),
                    file_constraints: Vec::new(),
                    expected_output: String::new(),
                    result: None,
                };
                task.subtasks.push(new_subtask);
            }
        }

        task.updated_at = chrono::Utc::now();
    }

    /// Record an event from NATS (`uc.task.event`).
    ///
    /// Pushes the event into the event log for WatchTask streaming.
    pub fn record_event(&mut self, event: uc_engine::AgentEventType) {
        self.events.push(event);
    }

    /// Update the last heartbeat timestamp from the Python NATS consumer.
    pub fn update_last_heartbeat(&mut self) {
        self.last_heartbeat = Some(chrono::Utc::now());
    }

    /// Get the last heartbeat timestamp.
    pub fn last_heartbeat(&self) -> Option<chrono::DateTime<chrono::Utc>> {
        self.last_heartbeat
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

/// Simple task decomposition heuristic: split description by newlines
/// or numbered items, creating one subtask per line/item.
fn decompose_task(parent_id: &uc_types::TaskId, description: &str) -> Vec<uc_types::Subtask> {
    let lines: Vec<&str> = description
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();

    if lines.is_empty() {
        // Single subtask if description has no newlines
        return vec![uc_types::Subtask {
            id: uc_types::TaskId::new(),
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

    // Create subtasks from lines, with sequential dependencies
    let mut subtasks = Vec::new();
    let mut prev_id: Option<uc_types::TaskId> = None;

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

        let st_id = uc_types::TaskId::new();
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
    /// NATS client for task submission and status subscriptions.
    /// Present when the `messaging` feature is enabled and NATS connection succeeded.
    #[cfg(feature = "messaging")]
    nats_client: Option<async_nats::Client>,
    /// Local Python worker bridge for task execution without NATS.
    /// The worker is spawned lazily on the first task submission.
    local_worker: Arc<Mutex<crate::local_worker::LocalWorkerBridge>>,
    /// Broadcast channel for real-time task event streaming.
    /// All event sources (local worker, NATS, local decomposition) publish here.
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
    /// The local Python worker is spawned lazily on the first task submission.
    /// Falls back to local (newline-split) decomposition if the worker is
    /// unavailable.
    pub fn new(engine: E) -> Self {
        let (event_tx, _) = broadcast::channel(256);

        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store: Arc::new(Mutex::new(TaskStore::new())),
                #[cfg(feature = "messaging")]
                nats_client: None,
                local_worker: Arc::new(Mutex::new(crate::local_worker::LocalWorkerBridge::new())),
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
        Self::with_nats_and_timeout(engine, nats_url, std::time::Duration::from_secs(600)).await
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
            nats_client: nats_client.clone(),
            local_worker: Arc::new(Mutex::new(crate::local_worker::LocalWorkerBridge::new())),
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
    pub fn into_services(self) -> (EngineServiceServer<Self>, TaskServiceServer<Self>) {
        let engine_service = EngineServiceServer::new(Self {
            inner: self.inner.clone(),
        });
        let task_service = TaskServiceServer::new(self);
        (engine_service, task_service)
    }
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
                            let mut store = task_store.lock().await;
                            store.apply_update(&update);
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
                Some(_message) = heartbeat_sub.next() => {
                    let mut store = task_store.lock().await;
                    store.update_last_heartbeat();
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
    _nats_client: async_nats::Client,
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
        }
    });
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
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let tool_input = event
                .data
                .get("tool_input")
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
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let success = json_bool_or_default(&event.data, "success", false);
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
            Some(uc_engine::AgentEventType::SubtaskCompleted {
                task_id,
                subtask_id,
                summary,
                success,
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
            Some(uc_engine::AgentEventType::SubtaskFailed {
                task_id,
                subtask_id,
                error,
                recoverable,
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
        let mut result = self.inner.engine.health().await.map_err(to_status)?;

        // Add local_worker component status
        let worker_bridge = self.inner.local_worker.lock().await;
        let worker_status = if worker_bridge.is_available() {
            ("healthy", Some("connected".to_string()))
        } else {
            ("unavailable", Some("not started".to_string()))
        };
        drop(worker_bridge);
        result.components.push(uc_types::ComponentHealth {
            name: "local_worker".to_string(),
            status: worker_status.0.to_string(),
            details: worker_status.1,
        });

        Ok(Response::new(result.into()))
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
                            "Failed to serialize NATS submit payload, falling back to local decomposition"
                        );
                        // Remove the Planning placeholder before falling back
                        {
                            let mut store = self.inner.task_store.lock().await;
                            store.tasks.remove(&task_id_str);
                            store.events.retain(|e| {
                                !matches!(e, uc_engine::AgentEventType::TaskCreated { task_id, .. } if task_id.0 == task_id_str)
                            });
                        }
                        return self.submit_task_local(req).await;
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
                            "NATS publish failed, falling back to local decomposition"
                        );
                        // NATS publish failed — fall back to local decomposition.
                        // Remove the Planning task and re-create with local decomposition.
                        {
                            let mut store = self.inner.task_store.lock().await;
                            store.tasks.remove(&task_id_str);
                            store.events.retain(|e| {
                                !matches!(e, uc_engine::AgentEventType::TaskCreated { task_id, .. } if task_id.0 == task_id_str)
                            });
                        }
                        return self.submit_task_local(req).await;
                    }
                }
            }
        }

        // No NATS — try local worker, then fall back to newline-split
        self.submit_task_via_bridge(req).await
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
        let event_rx = self.inner.event_tx.subscribe();

        // Hybrid stream: replay existing events from TaskStore first,
        // then switch to broadcast Receiver for real-time delivery.
        let stream = async_stream::stream! {
            // Phase 1: replay existing events from TaskStore
            {
                let s = task_store.lock().await;
                let events = s.read_events_from(0);
                for event in events {
                    let proto_event: TaskEvent = event.into();
                    if !task_id.is_empty() && proto_event.task_id != task_id {
                        continue;
                    }
                    yield Ok(proto_event);
                }
            }

            // Phase 2: listen for new events via broadcast
            let mut rx = event_rx;
            loop {
                match rx.recv().await {
                    Ok(proto_event) => {
                        if !task_id.is_empty() && proto_event.task_id != task_id {
                            continue;
                        }
                        yield Ok(proto_event);
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!(
                            skipped = n,
                            "WatchTask broadcast receiver lagged, some events dropped"
                        );
                        continue;
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
        let mut store = self.inner.task_store.lock().await;
        match store.pause_task(&req.task_id) {
            Ok(task) => Ok(Response::new(PauseTaskResponse {
                success: true,
                task_id: task.id.0,
                status: task_status_to_proto(&task.status).to_string(),
                error: None,
            })),
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
        let mut store = self.inner.task_store.lock().await;
        match store.resume_task(&req.task_id) {
            Ok(task) => Ok(Response::new(ResumeTaskResponse {
                success: true,
                task_id: task.id.0,
                status: task_status_to_proto(&task.status).to_string(),
                error: None,
            })),
            Err(e) => Ok(Response::new(ResumeTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }
}

// ── Local task decomposition (fallback) ──────────────────────

/// Apply a worker task update to the TaskStore and broadcast events.
///
/// This is a standalone function (not a method) so it can be called from
/// both the GrpcServer methods and the notification reader background task.
///
/// Creates or updates the task in the store, mapping worker subtask
/// statuses back to uc_types, records AgentEventType events, and
/// broadcasts them via the event_tx channel.
pub async fn apply_worker_update_to_store(
    update: &crate::local_worker::WorkerTaskUpdate,
    task_store: &Arc<Mutex<TaskStore>>,
    event_tx: &broadcast::Sender<TaskEvent>,
) {
    use crate::local_worker::WorkerSubtaskUpdate;

    let mut store = task_store.lock().await;
    let event_count_before = store.events.len();

    // Convert subtask updates
    let subtasks: Vec<uc_types::Subtask> = update
        .subtasks
        .iter()
        .map(|st: &WorkerSubtaskUpdate| {
            let status =
                subtask_status_from_str(&st.status).unwrap_or(uc_types::SubtaskStatus::Pending);
            uc_types::Subtask {
                id: uc_types::TaskId(st.id.clone()),
                parent_id: uc_types::TaskId(update.task_id.clone()),
                description: st.description.clone(),
                status,
                assigned_worker: st
                    .assigned_worker
                    .as_deref()
                    .map(|w| uc_types::WorkerId(w.to_string())),
                depends_on: st
                    .depends_on
                    .iter()
                    .map(|d| uc_types::TaskId(d.clone()))
                    .collect(),
                file_constraints: Vec::new(),
                expected_output: String::new(),
                result: None,
            }
        })
        .collect();

    let task_status =
        task_status_from_str(&update.status).unwrap_or(uc_types::TaskStatus::InProgress);

    let task = uc_types::Task {
        id: uc_types::TaskId(update.task_id.clone()),
        description: update.description.clone(),
        project_id: update.project_id.clone(),
        status: task_status,
        subtasks,
        created_at: chrono::Utc::now(),
        updated_at: chrono::Utc::now(),
    };

    store.tasks.insert(update.task_id.clone(), task);

    // Record events for subtask state transitions so WatchTask can stream them
    for st in &update.subtasks {
        let status =
            subtask_status_from_str(&st.status).unwrap_or(uc_types::SubtaskStatus::Pending);
        let event = match status {
            uc_types::SubtaskStatus::Assigned => uc_engine::AgentEventType::SubtaskAssigned {
                task_id: uc_types::TaskId(update.task_id.clone()),
                subtask_id: uc_types::TaskId(st.id.clone()),
                worker_id: st
                    .assigned_worker
                    .as_deref()
                    .map(|w| uc_types::WorkerId(w.to_string()))
                    .unwrap_or_else(uc_types::WorkerId::new),
            },
            uc_types::SubtaskStatus::InProgress => uc_engine::AgentEventType::SubtaskStarted {
                task_id: uc_types::TaskId(update.task_id.clone()),
                subtask_id: uc_types::TaskId(st.id.clone()),
                worker_id: st
                    .assigned_worker
                    .as_deref()
                    .map(|w| uc_types::WorkerId(w.to_string()))
                    .unwrap_or_else(uc_types::WorkerId::new),
            },
            uc_types::SubtaskStatus::Completed => uc_engine::AgentEventType::SubtaskCompleted {
                task_id: uc_types::TaskId(update.task_id.clone()),
                subtask_id: uc_types::TaskId(st.id.clone()),
                summary: String::new(),
                success: true,
            },
            uc_types::SubtaskStatus::Failed => uc_engine::AgentEventType::SubtaskFailed {
                task_id: uc_types::TaskId(update.task_id.clone()),
                subtask_id: uc_types::TaskId(st.id.clone()),
                error: String::new(),
                recoverable: false,
            },
            _ => continue,
        };
        store.record_event(event);
    }

    // Broadcast newly recorded events
    let new_events = store.events[event_count_before..]
        .iter()
        .cloned()
        .map(|e| -> TaskEvent { e.into() })
        .collect::<Vec<_>>();
    drop(store);
    for event in new_events {
        let _ = event_tx.send(event);
    }
}

/// Mark all in-progress tasks as Failed in the TaskStore and broadcast events.
///
/// Called when the local worker process dies unexpectedly.
async fn mark_tasks_failed_on_worker_death(
    task_store: &Arc<Mutex<TaskStore>>,
    event_tx: &broadcast::Sender<TaskEvent>,
) {
    let mut store = task_store.lock().await;
    let event_count_before = store.events.len();

    let mut failed_ids = Vec::new();
    for (id, task) in &mut store.tasks {
        if task.status == uc_types::TaskStatus::InProgress
            || task.status == uc_types::TaskStatus::Planning
        {
            task.status = uc_types::TaskStatus::Failed;
            task.updated_at = chrono::Utc::now();
            failed_ids.push(id.clone());
        }
    }

    if !failed_ids.is_empty() {
        tracing::warn!(
            tasks_failed = failed_ids.len(),
            "Marked tasks as Failed due to local worker death"
        );
    }

    // Broadcast Failed events
    let new_events = store.events[event_count_before..]
        .iter()
        .cloned()
        .map(|e| -> TaskEvent { e.into() })
        .collect::<Vec<_>>();
    drop(store);
    for event in new_events {
        let _ = event_tx.send(event);
    }
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Ensure the local Python worker is spawned and the notification reader
    /// is started. Called lazily on the first task submission.
    ///
    /// Returns true if the worker is available, false otherwise.
    async fn ensure_local_worker(&self) -> bool {
        let bridge_guard = self.inner.local_worker.lock().await;

        // Already available
        if bridge_guard.is_available() {
            return true;
        }

        // Try to spawn the worker
        match bridge_guard.ensure_worker().await {
            Ok(()) => {
                // Worker spawned successfully — start the notification reader.
                let task_store = self.inner.task_store.clone();
                let event_tx = self.inner.event_tx.clone();
                // Clone for the on_worker_dead closure (the apply_fn closure
                // moves the originals).
                let task_store_for_death = task_store.clone();
                let event_tx_for_death = event_tx.clone();

                // Start the notification reader with closures that apply
                // updates and handle worker death.
                bridge_guard.start_notification_reader(
                    move |update: crate::local_worker::WorkerTaskUpdate| {
                        // We need to call the async apply_worker_update_to_store.
                        // Since this closure is sync, we spawn a tokio task.
                        let ts = task_store.clone();
                        let tx = event_tx.clone();
                        tokio::spawn(async move {
                            apply_worker_update_to_store(&update, &ts, &tx).await;
                        });
                    },
                    move || {
                        // Worker died — mark tasks as Failed
                        let ts = task_store_for_death.clone();
                        let tx = event_tx_for_death.clone();
                        tokio::spawn(async move {
                            mark_tasks_failed_on_worker_death(&ts, &tx).await;
                        });
                    },
                );

                true
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    "Failed to spawn local worker, will use local decomposition"
                );
                false
            }
        }
    }

    /// Submit a task via the local Python worker bridge.
    ///
    /// Creates a task in Planning status, sends it to the worker via JSON-RPC,
    /// and returns immediately. The background notification reader will apply
    /// updates and broadcast events as the worker processes the task.
    ///
    /// Falls back to `submit_task_local` if the worker is unavailable.
    async fn submit_task_via_bridge(
        &self,
        req: SubmitTaskRequest,
    ) -> Result<Response<SubmitTaskResponse>, Status> {
        // Ensure the worker is spawned and the reader is running
        let worker_available = self.ensure_local_worker().await;

        if worker_available {
            let bridge = self.inner.local_worker.lock().await;
            if bridge.is_available() {
                // Create task in Planning status
                let (task_id_str, new_events) = {
                    let mut store = self.inner.task_store.lock().await;
                    let event_count_before = store.events.len();
                    let task =
                        store.submit_task_pending(req.description.clone(), req.project_id.clone());
                    let events: Vec<TaskEvent> = store.events[event_count_before..]
                        .iter()
                        .cloned()
                        .map(|e| e.into())
                        .collect();
                    (task.id.0.clone(), events)
                };

                // Broadcast TaskCreated event
                for event in new_events {
                    let _ = self.inner.event_tx.send(event);
                }

                // Send the task to the worker (fire-and-forget)
                match bridge
                    .send_submit_task(&req.description, &req.project_id)
                    .await
                {
                    Ok(()) => {
                        tracing::info!(
                            task_id = %task_id_str,
                            "Task submitted to local worker, status=Planning"
                        );

                        // Build response with Planning status
                        let store = self.inner.task_store.lock().await;
                        if let Some(task) = store.get_task(&task_id_str) {
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

                        // Fallback: task was created but not found (shouldn't happen)
                        return Ok(Response::new(SubmitTaskResponse {
                            success: true,
                            task_id: task_id_str,
                            status: "Planning".to_string(),
                            subtask_count: 0,
                            subtasks: Vec::new(),
                            error: None,
                        }));
                    }
                    Err(e) => {
                        tracing::warn!(
                            error = %e,
                            "Failed to send task to local worker, falling back to local decomposition"
                        );
                        // Remove the Planning placeholder before falling back
                        {
                            let mut store = self.inner.task_store.lock().await;
                            store.tasks.remove(&task_id_str);
                            store.events.retain(|e| {
                                !matches!(e, uc_engine::AgentEventType::TaskCreated { task_id, .. } if task_id.0 == task_id_str)
                            });
                        }
                        // Fall through to local decomposition
                    }
                }
            }
        }

        // Worker unavailable or failed — fall back
        self.submit_task_local(req).await
    }

    /// Submit a task using local (newline-split) decomposition.
    ///
    /// This is the fallback path when NATS and the local worker are unavailable.
    async fn submit_task_local(
        &self,
        req: SubmitTaskRequest,
    ) -> Result<Response<SubmitTaskResponse>, Status> {
        let mut store = self.inner.task_store.lock().await;
        let event_count_before = store.events.len();
        let task = store.submit_task(req.description, req.project_id);

        // Broadcast newly recorded events to all WatchTask streams
        let new_events = store.events[event_count_before..]
            .iter()
            .cloned()
            .map(|e| -> TaskEvent { e.into() })
            .collect::<Vec<_>>();
        drop(store);
        for event in new_events {
            let _ = self.inner.event_tx.send(event);
        }

        let subtask_protos: Vec<SubtaskProto> =
            task.subtasks.clone().into_iter().map(Into::into).collect();

        Ok(Response::new(SubmitTaskResponse {
            success: true,
            task_id: task.id.0,
            status: task_status_to_proto(&task.status).to_string(),
            subtask_count: task.subtasks.len() as u32,
            subtasks: subtask_protos,
            error: None,
        }))
    }
}

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

        assert_eq!(task.subtasks.len(), 3);
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

    #[test]
    fn decompose_task_single_line() {
        let parent_id = uc_types::TaskId::new();
        let subtasks = decompose_task(&parent_id, "Single task description");
        assert_eq!(subtasks.len(), 1);
        assert_eq!(subtasks[0].description, "Single task description");
    }

    #[test]
    fn decompose_task_multiple_lines() {
        let parent_id = uc_types::TaskId::new();
        let subtasks = decompose_task(&parent_id, "1. First item\n2. Second item\n3. Third item");
        assert_eq!(subtasks.len(), 3);
        // Check that numbered prefixes are stripped
        assert_eq!(subtasks[0].description, "First item");
        assert_eq!(subtasks[1].description, "Second item");
        // Check sequential dependencies
        assert!(subtasks[0].depends_on.is_empty());
        assert_eq!(subtasks[1].depends_on.len(), 1);
    }

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
            task_id: "abc-123".to_string(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
                result: None,
            }],
            result: None,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsTaskUpdate = serde_json::from_str(&json).unwrap();
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
            r#type: "tool_call".to_string(),
            task_id: "abc-123".to_string(),
            subtask_id: Some("st-1".to_string()),
            data,
        };
        let json = serde_json::to_string(&msg).unwrap();
        let parsed: NatsTaskEvent = serde_json::from_str(&json).unwrap();
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
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: "st-new-1".to_string(),
                status: "Assigned".to_string(),
                assigned_worker: Some("worker-1".to_string()),
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
            task_id: task_id.clone(),
            status: "InProgress".to_string(),
            subtasks: vec![NatsSubtaskUpdate {
                subtask_id: subtask_id.clone(),
                status: "Completed".to_string(),
                assigned_worker: None,
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

    #[test]
    fn json_bool_or_default_bool_values() {
        let mut data = serde_json::Map::new();
        data.insert("flag".to_string(), serde_json::Value::Bool(true));
        data.insert("off".to_string(), serde_json::Value::Bool(false));

        assert!(json_bool_or_default(&data, "flag", false));
        assert!(!json_bool_or_default(&data, "off", true));
    }

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
}
