//! OrchestratorDispatcher — NATS-based dispatcher for scheduled tasks.
//!
//! When a scheduled task fires (and the night-window guard passes),
//! the `OrchestratorDispatcher` publishes a NATS message to
//! `schedule.trigger.{task_id}` with the `ScheduledTask` as JSON payload.
//!
//! The Python Orchestrator subscribes to `schedule.trigger.>` and
//! calls `submit_task()` for each triggered event.
//!
//! If NATS is not available, falls back to logging (graceful degradation).
//! Feature-gated behind `messaging` (NATS dependency).

use tracing::info;
#[cfg(feature = "messaging")]
use tracing::warn;
use uc_types::{EngineError, ScheduledTask};

use super::service::ScheduleDispatcher;

// ── OrchestratorDispatcher (with NATS) ──────────────────────────

/// NATS-based dispatcher that publishes schedule trigger events.
///
/// When a scheduled task fires, it publishes a JSON payload to
/// `schedule.trigger.{task_id}` on NATS. The Python Orchestrator
/// subscribes to this subject pattern and executes the task.
///
/// If NATS connectivity fails, it logs a warning and does not
/// propagate the error — graceful degradation.
#[cfg(feature = "messaging")]
pub struct OrchestratorDispatcher {
    nats_client: async_nats::Client,
    subject_prefix: String,
}

#[cfg(feature = "messaging")]
impl OrchestratorDispatcher {
    /// Create a new OrchestratorDispatcher with a NATS client.
    ///
    /// Args:
    ///     nats_client: An established async-nats client.
    ///     subject_prefix: Prefix for NATS subjects (default: "schedule.trigger").
    pub fn new(nats_client: async_nats::Client, subject_prefix: Option<String>) -> Self {
        Self {
            nats_client,
            subject_prefix: subject_prefix.unwrap_or_else(|| "schedule.trigger".to_string()),
        }
    }

    /// Create a new OrchestratorDispatcher with default subject prefix.
    pub fn with_client(nats_client: async_nats::Client) -> Self {
        Self::new(nats_client, None)
    }
}

#[cfg(feature = "messaging")]
impl ScheduleDispatcher for OrchestratorDispatcher {
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        let subject = format!("{}.{}", self.subject_prefix, task.id);
        let payload = serde_json::to_vec(task).map_err(|e| {
            EngineError::InternalError(format!(
                "Failed to serialize scheduled task {}: {}",
                task.id, e
            ))
        })?;

        info!(
            task_id = %task.id,
            subject = %subject,
            description = %task.description,
            "Dispatching scheduled task via NATS request-reply"
        );

        // Send uc.task.submit as a request-reply to get subtask decomposition back.
        // The Python NatsWorker handles this and replies with the decomposed task JSON.
        let client = self.nats_client.clone();
        let timeout_secs: u64 = 120;
        let result = tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current().block_on(async {
                tokio::time::timeout(
                    std::time::Duration::from_secs(timeout_secs),
                    client.request(subject, payload.into()),
                )
                .await
            })
        });

        match result {
            Ok(Ok(reply)) => {
                // Parse the reply to extract subtasks, resolve dependencies,
                // and publish per-layer execution requests.
                match self._process_decomposition_reply(task, &reply) {
                    Ok(()) => {
                        info!(task_id = %task.id, "Scheduled task dispatched successfully");
                        Ok(())
                    }
                    Err(e) => {
                        warn!(task_id = %task.id, error = %e, "Decomposition reply processing failed");
                        Err(e)
                    }
                }
            }
            Ok(Err(e)) => {
                // NATS failed — no worker received the task. Surface as Err
                // so dispatch_with_guard records Skipped (not Completed, which
                // previously hid silent dispatch loss — task appeared done
                // with nothing having run).
                warn!(task_id = %task.id, error = %e, "NATS request failed (no worker received task)");
                Err(EngineError::TaskError(format!(
                    "NATS dispatch failed: {}",
                    e
                )))
            }
            Err(_) => {
                warn!(task_id = %task.id, timeout_secs, "NATS request-reply timed out (no worker received task)");
                Err(EngineError::TaskError(format!(
                    "NATS dispatch timed out after {}s",
                    timeout_secs
                )))
            }
        }
    }
}

/// Parse the `subtasks` array from a decomposition reply into typed `Subtask`s.
///
/// Returns `Err` if ANY subtask fails to deserialize — do NOT silently drop
/// parse failures. A dropped subtask whose ID is referenced by a surviving
/// subtask's `depends_on` would have that dependency silently ignored by
/// `dependency::resolve_execution_order` ("Unknown dependency → ignore"),
/// yielding wrong execution order / data corruption. Surfacing the failure
/// marks the task failed instead of dispatching with a corrupted graph.
fn parse_subtasks(subtasks: &[serde_json::Value]) -> Result<Vec<uc_types::Subtask>, EngineError> {
    let mut parsed: Vec<uc_types::Subtask> = Vec::with_capacity(subtasks.len());
    for (i, v) in subtasks.iter().enumerate() {
        match serde_json::from_value::<uc_types::Subtask>(v.clone()) {
            Ok(st) => parsed.push(st),
            Err(e) => {
                return Err(EngineError::TaskError(format!(
                    "Failed to parse subtask at index {i} from decomposition reply: {e}"
                )));
            }
        }
    }
    Ok(parsed)
}

#[cfg(feature = "messaging")]
impl OrchestratorDispatcher {
    /// Process the decomposition reply from the Orchestrator.
    ///
    /// The reply contains the decomposed task with subtasks. We resolve
    /// the dependency order and publish `uc.subtask.execute` per layer.
    fn _process_decomposition_reply(
        &self,
        _task: &ScheduledTask,
        reply: &async_nats::Message,
    ) -> Result<(), EngineError> {
        let reply_data: serde_json::Value = serde_json::from_slice(&reply.payload)
            .map_err(|e| EngineError::InternalError(format!("Invalid reply JSON: {e}")))?;

        let subtasks = reply_data
            .get("subtasks")
            .and_then(|v| v.as_array())
            .ok_or_else(|| EngineError::TaskError("Reply missing subtasks array".into()))?;

        if subtasks.is_empty() {
            info!("No subtasks in decomposition reply — nothing to dispatch");
            return Ok(());
        }

        // Parse subtasks into uc_types::Subtask for dependency resolution.
        let parsed = parse_subtasks(subtasks)?;

        // Resolve dependency order
        let layers = super::dependency::resolve_execution_order(&parsed)?;

        info!(
            layer_count = layers.len(),
            total_subtasks = parsed.len(),
            "Resolved subtask execution order"
        );

        // Publish each layer as uc.subtask.execute messages.
        // In the current synchronous dispatch, we publish all layers at once.
        // The Worker/NatsWorker will respect dependencies based on depends_on.
        // ponytail: full event-driven layer-wait would require async dispatch;
        // for now, publish all with dependency info and let the Worker handle ordering.
        let client = self.nats_client.clone();
        for (layer_idx, layer) in layers.iter().enumerate() {
            for subtask_id in layer {
                let st = parsed.iter().find(|s| &s.id == subtask_id);
                let desc = st.map(|s| s.description.as_str()).unwrap_or("");
                let msg = serde_json::json!({
                    "task_id": reply_data.get("task_id").and_then(|v| v.as_str()).unwrap_or(""),
                    "subtask_id": subtask_id.0,
                    "description": desc,
                    "layer": layer_idx,
                });
                let subject = "uc.subtask.execute".to_string();
                let payload = serde_json::to_vec(&msg).map_err(|e| {
                    EngineError::InternalError(format!("Failed to serialize subtask: {e}"))
                })?;
                let c = client.clone();
                // dispatch() already runs inside block_in_place (see L78), so we must
                // NOT nest another block_in_place here — that panics. block_on alone is
                // legal within a block_in_place context.
                //
                // Do NOT swallow the publish error: a dropped publish leaves the
                // subtask Assigned in the registry but never delivered to
                // uc.subtask.execute, so it stalls until the stale-assigned reaper
                // reverts it. Surface the failure so dispatch() logs it + marks the
                // task failed (caller at dispatcher.rs:98).
                if let Err(e) = tokio::runtime::Handle::current()
                    .block_on(async { c.publish(subject, payload.into()).await })
                {
                    return Err(EngineError::ConnectionError(format!(
                        "NATS publish failed for subtask {:?}: {e}",
                        subtask_id
                    )));
                }
            }
        }

        Ok(())
    }
}

#[cfg(feature = "messaging")]
impl std::fmt::Debug for OrchestratorDispatcher {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OrchestratorDispatcher")
            .field("subject_prefix", &self.subject_prefix)
            .finish_non_exhaustive()
    }
}

// ── OrchestratorDispatcher (without NATS — fallback to logging) ──

/// Logging-only dispatcher when NATS is not available.
///
/// This is functionally identical to `LoggingDispatcher` but exists
/// as a named type for clarity: it represents the OrchestratorDispatcher
/// in deployments without NATS messaging.
#[cfg(not(feature = "messaging"))]
pub struct OrchestratorDispatcher;

#[cfg(not(feature = "messaging"))]
impl OrchestratorDispatcher {
    /// Create a new logging-only OrchestratorDispatcher.
    ///
    /// Since NATS is not available (messaging feature disabled),
    /// all dispatches will be logged only.
    pub fn new() -> Self {
        Self
    }
}

#[cfg(not(feature = "messaging"))]
impl Default for OrchestratorDispatcher {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(not(feature = "messaging"))]
impl ScheduleDispatcher for OrchestratorDispatcher {
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        info!(
            task_id = %task.id,
            description = %task.description,
            project_id = %task.project_id,
            "Scheduled task dispatched (NATS unavailable, logging only)"
        );
        Ok(())
    }
}

// ── Window event publisher ──────────────────────────────────────

/// Publish night-window status events to NATS.
///
/// When the night window opens or closes, these events are published
/// so the Python Orchestrator can toggle its `night_window_active` flag.
///
/// Subjects:
/// - `schedule.window.opened` — night window has started
/// - `schedule.window.closed` — night window has ended
#[cfg(feature = "messaging")]
pub async fn publish_window_event(
    nats_client: &async_nats::Client,
    event_type: WindowEventType,
    window_info: &str,
) -> Result<(), EngineError> {
    let subject = match event_type {
        WindowEventType::Opened => "schedule.window.opened",
        WindowEventType::Closed => "schedule.window.closed",
    };

    let payload = serde_json::json!({
        "event": match event_type {
            WindowEventType::Opened => "opened",
            WindowEventType::Closed => "closed",
        },
        "window_info": window_info,
        "timestamp": chrono::Utc::now().to_rfc3339(),
    });

    let payload_bytes = serde_json::to_vec(&payload).map_err(|e| {
        EngineError::InternalError(format!("Failed to serialize window event: {}", e))
    })?;

    nats_client
        .publish(subject.to_string(), payload_bytes.into())
        .await
        .map_err(|e| {
            warn!(error = %e, "Failed to publish window event to NATS");
            EngineError::ConnectionError(format!("NATS publish failed: {}", e))
        })?;

    info!(
        event_type = %subject,
        "Window event published to NATS"
    );
    Ok(())
}

/// Type of night-window event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowEventType {
    /// Night window has opened (scheduled tasks can now run).
    Opened,
    /// Night window has closed (real-time tasks resume).
    Closed,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scheduler::LoggingDispatcher;
    use chrono::NaiveTime;
    use uc_types::ScheduledTask;

    fn make_cron_task() -> ScheduledTask {
        ScheduledTask::cron(
            "Test dispatch".to_string(),
            "test-project".to_string(),
            "0 22 * * *".to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    #[test]
    fn logging_dispatcher_works() {
        let dispatcher = LoggingDispatcher;
        let task = make_cron_task();
        let result = dispatcher.dispatch(&task);
        assert!(result.is_ok());
    }

    #[cfg(not(feature = "messaging"))]
    #[test]
    fn orchestrator_dispatcher_without_nats() {
        let dispatcher = OrchestratorDispatcher::new();
        let task = make_cron_task();
        let result = dispatcher.dispatch(&task);
        assert!(result.is_ok());
    }

    #[test]
    fn window_event_type_display() {
        assert_eq!(WindowEventType::Opened, WindowEventType::Opened);
        assert_eq!(WindowEventType::Closed, WindowEventType::Closed);
        assert_ne!(WindowEventType::Opened, WindowEventType::Closed);
    }

    #[test]
    fn parse_subtasks_partial_drop_returns_err() {
        // One valid + one malformed subtask (depends_on as int, not array).
        // Previously the malformed one was silently dropped and dispatch
        // proceeded with a corrupted dependency graph.
        use uc_types::{Subtask, SubtaskStatus, TaskId};
        let good = serde_json::to_value(&Subtask {
            id: TaskId("st-a".into()),
            parent_id: TaskId("t-1".into()),
            description: "a".into(),
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: vec![],
            file_constraints: vec![],
            expected_output: String::new(),
            result: None,
            dispatch_mode: uc_types::DispatchMode::default(),
            dispatch_retry_count: 0,
            required_capabilities: vec![],
            agent_config_json: None,
            steps: vec![],
            retry_count: 0,
        })
        .unwrap();
        let bad = serde_json::json!({"id":"st-b","depends_on":1});
        let subtasks = vec![good, bad];
        let result = parse_subtasks(&subtasks);
        assert!(result.is_err(), "partial parse failure must surface as Err");
        let msg = format!("{}", result.unwrap_err());
        assert!(
            msg.contains("index 1"),
            "err must name the failed index: {msg}"
        );
    }

    #[test]
    fn parse_subtasks_all_valid() {
        use uc_types::{Subtask, SubtaskStatus, TaskId};
        let make = |id: &str, deps: Vec<TaskId>| {
            serde_json::to_value(&Subtask {
                id: TaskId(id.into()),
                parent_id: TaskId("t-1".into()),
                description: id.into(),
                status: SubtaskStatus::Pending,
                assigned_worker: None,
                depends_on: deps,
                file_constraints: vec![],
                expected_output: String::new(),
                result: None,
                dispatch_mode: uc_types::DispatchMode::default(),
                dispatch_retry_count: 0,
                required_capabilities: vec![],
                agent_config_json: None,
                steps: vec![],
                retry_count: 0,
            })
            .unwrap()
        };
        let subtasks = vec![
            make("st-a", vec![]),
            make("st-b", vec![TaskId("st-a".into())]),
        ];
        let parsed = parse_subtasks(&subtasks).expect("valid subtasks parse");
        assert_eq!(parsed.len(), 2);
        assert_eq!(parsed[1].depends_on.len(), 1);
    }
}
