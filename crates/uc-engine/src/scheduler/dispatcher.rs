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

        // Publish is async; we use a blocking approach here since
        // ScheduleDispatcher::dispatch is currently a sync trait method.
        // The dispatch is fire-and-forget — if NATS is temporarily
        // unavailable, we log and continue (graceful degradation).
        info!(
            task_id = %task.id,
            subject = %subject,
            "Publishing schedule trigger event to NATS"
        );

        // Try to publish synchronously via tokio runtime
        let client = self.nats_client.clone();
        let result = tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current()
                .block_on(async { client.publish(subject, payload.into()).await })
        });

        match result {
            Ok(()) => {
                info!(
                    task_id = %task.id,
                    "Schedule trigger event published successfully"
                );
                Ok(())
            }
            Err(e) => {
                // Graceful degradation: log warning but don't fail the dispatch
                // The task will still be recorded in execution history.
                warn!(
                    task_id = %task.id,
                    error = %e,
                    "Failed to publish schedule trigger to NATS (graceful degradation)"
                );
                // Return Ok so execution history is recorded as Completed
                Ok(())
            }
        }
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
}
