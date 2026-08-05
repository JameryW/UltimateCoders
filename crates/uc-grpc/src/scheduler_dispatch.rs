//! NATS-based scheduler dispatcher.
//!
//! [`NatsSubmitDispatcher`] routes scheduler cron-fires through the
//! `uc.task.submit` NATS subject — the same path the gRPC `TaskService`
//! uses for interactive task submissions. The Python `NatsWorker`
//! subscribes to this subject, decomposes the task, and dispatches
//! subtasks via JetStream, so scheduler-created tasks enter the normal
//! decompose → JetStream dispatch → worker execution chain.
//!
//! This dispatcher lives in `uc-grpc` (not `uc-engine`) because it needs
//! `async-nats` access, which is a layer violation for `uc-engine`.
//! [`EngineSubmitDispatcher`](uc_engine::scheduler::EngineSubmitDispatcher)
//! stays in `uc-engine` as the no-NATS fallback.

#[cfg(feature = "messaging")]
use std::sync::Arc;

#[cfg(feature = "messaging")]
use tracing::{info, warn};
use uc_engine::scheduler::ScheduleDispatcher;
#[cfg(feature = "messaging")]
use uc_engine::scheduler::SchedulerService;
use uc_types::{EngineError, ScheduledTask};

#[cfg(feature = "messaging")]
use uc_types::{ExecutionHistory, ExecutionStatus};
#[cfg(feature = "messaging")]
use uuid::Uuid;

#[cfg(feature = "messaging")]
use crate::server::{NatsTaskSubmit, NATS_SUBJECT_TASK_SUBMIT};

/// NATS-based dispatcher that publishes scheduled-task fires to
/// `uc.task.submit`.
///
/// When a cron job or one-shot fires (and the night-window guard passes),
/// this dispatcher publishes a [`NatsTaskSubmit`] payload to NATS. The
/// Python `NatsWorker` consumes it, decomposes the task into subtasks,
/// and dispatches them via JetStream — the same path as interactive
/// `TaskService::submit_task`.
///
/// # Fire-and-forget semantics
///
/// `ScheduleDispatcher::dispatch` is sync, but NATS `publish` is async.
/// Like [`EngineSubmitDispatcher`], we spawn a tokio task for the publish
/// and return `Ok(())` immediately (spawn-success = `Completed`
/// `ExecutionHistory`). If the publish later fails, a `Failed`
/// `ExecutionHistory` is appended via the engine's `scheduler_service`
/// so the failure is visible in the dashboard history.
///
/// # Task ID
///
/// The published `task_id` is a **fresh** UUID — not the `ScheduledTask.id`
/// (which is the *scheduled* task's id, not the submitted task's id). This
/// mirrors the gRPC `TaskService::submit_task` path, which generates a new
/// `TaskId::new()` for each submission. The Python consumer uses this id
/// as the new task's id (or generates its own if absent).
///
/// [`EngineSubmitDispatcher`]: uc_engine::scheduler::EngineSubmitDispatcher
#[cfg(feature = "messaging")]
pub struct NatsSubmitDispatcher {
    nats_client: async_nats::Client,
    /// Scheduler service for recording Failed ExecutionHistory on publish error.
    scheduler: Arc<SchedulerService>,
}

#[cfg(feature = "messaging")]
impl NatsSubmitDispatcher {
    /// Create a new `NatsSubmitDispatcher`.
    ///
    /// # Arguments
    /// * `nats_client` — An established async-nats client.
    /// * `scheduler` — The scheduler service, used to append `Failed`
    ///   `ExecutionHistory` when the NATS publish fails asynchronously
    ///   (mirrors `EngineSubmitDispatcher`).
    pub fn new(nats_client: async_nats::Client, scheduler: Arc<SchedulerService>) -> Self {
        Self {
            nats_client,
            scheduler,
        }
    }

    /// Build the `uc.task.submit` payload for a scheduled task.
    ///
    /// Generates a **fresh** UUID for `task_id` (not `task.id`, which is the
    /// *scheduled* task's id). Mirrors `TaskService::submit_task` which calls
    /// `TaskId::new()` / `submit_task_pending`.
    ///
    /// Exposed as a separate method so tests can verify the payload shape
    /// without needing a real NATS connection.
    fn build_payload(task: &ScheduledTask) -> NatsTaskSubmit {
        NatsTaskSubmit {
            task_id: Uuid::new_v4().to_string(),
            description: task.description.clone(),
            project_id: task.project_id.clone(),
            verify_command: task.verify_command.clone(),
        }
    }

    /// Build the `Failed` `ExecutionHistory` entry for a publish failure.
    ///
    /// Exposed for testing — mirrors `EngineSubmitDispatcher`'s Failed-history
    /// construction.
    fn build_failed_history(
        scheduled_task_id: uuid::Uuid,
        started_at: chrono::DateTime<chrono::Utc>,
        error: &str,
    ) -> ExecutionHistory {
        ExecutionHistory {
            id: Uuid::new_v4(),
            scheduled_task_id,
            started_at,
            completed_at: Some(chrono::Utc::now()),
            status: ExecutionStatus::Failed,
            result_summary: Some(format!(
                "NatsSubmitDispatcher: NATS publish failed: {}",
                error
            )),
            deferred_reason: None,
        }
    }
}

#[cfg(feature = "messaging")]
impl ScheduleDispatcher for NatsSubmitDispatcher {
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        let payload = Self::build_payload(task);
        let task_id = Uuid::parse_str(&payload.task_id).map_err(|e| {
            EngineError::InternalError(format!(
                "NatsSubmitDispatcher: generated invalid task_id UUID: {}",
                e
            ))
        })?;
        let payload_bytes = serde_json::to_vec(&payload).map_err(|e| {
            EngineError::InternalError(format!(
                "NatsSubmitDispatcher: failed to serialize payload for scheduled task {}: {}",
                task.id, e
            ))
        })?;

        let scheduled_task_id = task.id;
        let client = self.nats_client.clone();
        let scheduler = self.scheduler.clone();
        let started_at = chrono::Utc::now();

        info!(
            scheduled_task_id = %scheduled_task_id,
            submitted_task_id = %task_id,
            subject = NATS_SUBJECT_TASK_SUBMIT,
            description = %task.description,
            "NatsSubmitDispatcher: publishing scheduled task to uc.task.submit"
        );

        // Fire-and-forget: spawn the async publish and return Ok immediately.
        // SchedulerService::dispatch_with_guard records Completed when dispatch
        // returns Ok (meaning "publish spawned"). If publish fails in the
        // background, we append a Failed ExecutionHistory (mirror EngineSubmitDispatcher).
        tokio::spawn(async move {
            match client
                .publish(NATS_SUBJECT_TASK_SUBMIT.to_string(), payload_bytes.into())
                .await
            {
                Ok(()) => {
                    info!(
                        scheduled_task_id = %scheduled_task_id,
                        submitted_task_id = %task_id,
                        "NatsSubmitDispatcher: published to uc.task.submit successfully"
                    );
                }
                Err(e) => {
                    warn!(
                        scheduled_task_id = %scheduled_task_id,
                        submitted_task_id = %task_id,
                        error = %e,
                        "NatsSubmitDispatcher: NATS publish failed for scheduled task"
                    );
                    // Append Failed ExecutionHistory so the failure is visible
                    // (the Completed entry from dispatch_with_guard means
                    // "spawn succeeded", this means "publish failed").
                    let history =
                        Self::build_failed_history(scheduled_task_id, started_at, &e.to_string());
                    scheduler.record_execution(&history).await;
                }
            }
        });

        Ok(())
    }
}

#[cfg(feature = "messaging")]
impl std::fmt::Debug for NatsSubmitDispatcher {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NatsSubmitDispatcher")
            .finish_non_exhaustive()
    }
}

// ── No-NATS stub (feature-gated) ─────────────────────────────────

/// Stub when `messaging` is disabled. `NatsSubmitDispatcher` requires NATS
/// — use [`EngineSubmitDispatcher`](uc_engine::scheduler::EngineSubmitDispatcher)
/// instead in no-NATS builds.
#[cfg(not(feature = "messaging"))]
pub struct NatsSubmitDispatcher;

#[cfg(not(feature = "messaging"))]
impl NatsSubmitDispatcher {
    /// NatsSubmitDispatcher requires the `messaging` feature.
    pub fn new() -> Self {
        Self
    }
}

#[cfg(not(feature = "messaging"))]
impl Default for NatsSubmitDispatcher {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(not(feature = "messaging"))]
impl ScheduleDispatcher for NatsSubmitDispatcher {
    fn dispatch(&self, _task: &ScheduledTask) -> Result<(), EngineError> {
        Err(EngineError::InternalError(
            "NatsSubmitDispatcher requires the messaging feature".to_string(),
        ))
    }
}

// ── NatsKvLockProvider (distributed lock via NATS KV) ─────────────

/// NATS KV-based distributed lock provider for multi-instance scheduler coordination.
///
/// Uses a NATS JetStream key-value store (`scheduler-locks` bucket) with a TTL
/// (`max_age`) to implement a per-tick distributed lease. When a cron job fires
/// on multiple gateway instances simultaneously, only the first instance to call
/// `try_acquire` succeeds (via `kv.create`, which fails if the key already exists).
/// The lock auto-expires after the TTL, so a crashed holder does not block future
/// ticks.
///
/// # Lock semantics
///
/// `kv.create(key, value)` fails if the key exists (not deleted/purged). This is
/// the acquire: success = this instance fires, failure = another instance holds
/// the lock → skip. The `max_age` config on the KV bucket ensures the key
/// auto-expires after the TTL, so the lock auto-releases even if the holder crashes.
///
/// # Fallback
///
/// If the KV store is unavailable or the `create` call errors for any reason
/// other than "already exists", the provider logs a warning and **acquires**
/// (returns `true`). This is the safe default: a transient NATS failure should
/// not prevent the scheduler from firing (better to risk a duplicate than to
/// miss a scheduled task entirely). The "already exists" error is the only
/// case that returns `false` (another instance legitimately holds the lock).
///
/// # Bucket creation
///
/// The KV bucket is created lazily on first `try_acquire` call (or on `new`).
/// `create_key_value` is idempotent — if the bucket already exists, it returns
/// the existing store. The `max_age` is set to a generous upper bound (e.g. 5
/// minutes) so expired keys are purged by the server; the per-key TTL is
/// enforced by the key value itself expiring.
#[cfg(feature = "messaging")]
pub struct NatsKvLockProvider {
    /// The NATS client used to access JetStream KV.
    nats_client: async_nats::Client,
    /// Unique identifier for this gateway instance (stored as the lock value
    /// for debugging — "which instance holds this lock?").
    instance_id: String,
}

/// The NATS KV bucket name for scheduler locks.
#[cfg(feature = "messaging")]
const SCHEDULER_LOCKS_BUCKET: &str = "scheduler-locks";

#[cfg(feature = "messaging")]
impl NatsKvLockProvider {
    /// Create a new `NatsKvLockProvider`.
    ///
    /// # Arguments
    /// * `nats_client` — An established async-nats client.
    /// * `instance_id` — Unique identifier for this gateway instance (stored
    ///   as the lock value for debugging).
    pub fn new(nats_client: async_nats::Client, instance_id: String) -> Self {
        Self {
            nats_client,
            instance_id,
        }
    }

    /// Create a new `NatsKvLockProvider` with a random instance ID.
    pub fn with_random_instance_id(nats_client: async_nats::Client) -> Self {
        Self::new(nats_client, Uuid::new_v4().to_string())
    }

    /// Build the lock key for a given scheduler tick.
    ///
    /// Exposed for testing — verifies the key format matches what the
    /// `SchedulerService` cron callback constructs.
    #[cfg(test)]
    fn build_lock_key(task_id: &str, tick_timestamp: i64) -> String {
        format!("scheduler:{}:{}", task_id, tick_timestamp)
    }
}

#[cfg(feature = "messaging")]
impl uc_engine::scheduler::LockProvider for NatsKvLockProvider {
    fn try_acquire(&self, key: &str, ttl: std::time::Duration) -> bool {
        // The LockProvider trait is sync, but NATS KV operations are async.
        // We use block_in_place + block_on (same pattern as OrchestratorDispatcher::dispatch).
        // This is called only on cron-fire (not per-request), so blocking a
        // runtime worker briefly is acceptable.
        //
        // Clone the key + instance_id into owned data so the async block
        // does not borrow from the method body (which would fail lifetime
        // checks with block_on's 'static requirement).
        let key = key.to_string();
        let instance_id_bytes = self.instance_id.clone().into_bytes();
        let nats_client = self.nats_client.clone();

        tokio::task::block_in_place(|| {
            tokio::runtime::Handle::current().block_on(async move {
                // Get or create the KV store. We can't use the OnceCell here
                // (borrowed from &self, which doesn't outlive block_on),
                // so we create/get the store directly on each call. This is
                // a minor overhead (one JetStream API call per cron-fire),
                // acceptable since cron-fires are infrequent (seconds+ apart).
                let js = async_nats::jetstream::new(nats_client);
                let kv = match js
                    .create_key_value(async_nats::jetstream::kv::Config {
                        bucket: SCHEDULER_LOCKS_BUCKET.to_string(),
                        description: "Distributed scheduler locks for multi-instance gateways"
                            .to_string(),
                        max_age: std::time::Duration::from_secs(300), // 5 min bucket-level TTL
                        ..Default::default()
                    })
                    .await
                {
                    Ok(store) => store,
                    Err(e) => {
                        warn!(
                            key = %key,
                            error = %e,
                            "NatsKvLockProvider: failed to access KV store — acquiring (safe default)"
                        );
                        return true;
                    }
                };

                // Use `create` — fails if the key already exists (not expired).
                // The value is the instance_id (for debugging — "who holds this lock?").
                match kv.create(key.as_str(), instance_id_bytes.clone().into()).await {
                    Ok(_revision) => {
                        info!(
                            key = %key,
                            ttl_secs = ttl.as_secs(),
                            "NatsKvLockProvider: lock acquired"
                        );
                        true
                    }
                    Err(e) => {
                        // Check if this is an "already exists" error (lock held
                        // by another instance). async-nats' CreateError is
                        // Error<CreateErrorKind>; AlreadyExists is the expected case.
                        use async_nats::jetstream::kv::CreateErrorKind;
                        if matches!(e.kind(), CreateErrorKind::AlreadyExists) {
                            // Another instance holds the lock — skip (expected, not an error)
                            info!(
                                key = %key,
                                "NatsKvLockProvider: lock held by another instance — skipping"
                            );
                            false
                        } else {
                            // Unexpected error — safe default is to acquire
                            // (don't let a transient NATS issue block the scheduler)
                            warn!(
                                key = %key,
                                error = %e,
                                "NatsKvLockProvider: unexpected error — acquiring (safe default)"
                            );
                            true
                        }
                    }
                }
            })
        })
    }
}

#[cfg(feature = "messaging")]
impl std::fmt::Debug for NatsKvLockProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NatsKvLockProvider")
            .field("instance_id", &self.instance_id)
            .field("bucket", &SCHEDULER_LOCKS_BUCKET)
            .finish_non_exhaustive()
    }
}

/// Stub when `messaging` is disabled. Use
/// [`NoOpLockProvider`](uc_engine::scheduler::NoOpLockProvider) instead.
#[cfg(not(feature = "messaging"))]
pub struct NatsKvLockProvider;

#[cfg(not(feature = "messaging"))]
impl NatsKvLockProvider {
    /// NatsKvLockProvider requires the `messaging` feature.
    pub fn new() -> Self {
        Self
    }
}

#[cfg(not(feature = "messaging"))]
impl Default for NatsKvLockProvider {
    fn default() -> Self {
        Self::new()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveTime;
    use uc_types::ScheduledTask;

    fn make_cron_task() -> ScheduledTask {
        ScheduledTask::cron(
            "Test scheduled dispatch".to_string(),
            "test-project".to_string(),
            "0 22 * * *".to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    // ── Tests ──────────────────────────────────────────────────────

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_payload_shape() {
        // Verify the payload we construct matches the NatsTaskSubmit contract
        // (task_id, description, project_id) — the same shape TaskService uses
        // and the Python _handle_submit consumer expects.
        let task = make_cron_task();
        let payload = NatsSubmitDispatcher::build_payload(&task);
        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        // Must have these fields (matches Python _handle_submit). verify_command
        // is optional (skip_serializing_if = None) so it's absent here.
        assert!(parsed.get("task_id").is_some(), "payload must have task_id");
        assert!(
            parsed.get("description").is_some(),
            "payload must have description"
        );
        assert!(
            parsed.get("project_id").is_some(),
            "payload must have project_id"
        );
        assert_eq!(
            parsed["description"], "Test scheduled dispatch",
            "description must match the scheduled task"
        );
        assert_eq!(
            parsed["project_id"], "test-project",
            "project_id must match the scheduled task"
        );
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_generates_fresh_task_id() {
        // The published task_id must be a fresh UUID, NOT the scheduled task's id.
        // (The scheduled task's id is the SCHEDULE id, not the submitted task id.)
        let task = make_cron_task();
        let scheduled_id = task.id;

        let payload = NatsSubmitDispatcher::build_payload(&task);

        let parsed_task_id: Uuid = payload.task_id.parse().expect("task_id is a valid UUID");
        assert_ne!(
            parsed_task_id, scheduled_id,
            "published task_id must NOT be the scheduled task's id"
        );
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_payload_includes_verify_command_when_set() {
        // When the ScheduledTask has a verify_command, the payload must
        // include it so the Python aggregator runs verification.
        let mut task = make_cron_task();
        task.verify_command = Some("cargo check".to_string());
        let payload = NatsSubmitDispatcher::build_payload(&task);
        assert_eq!(
            payload.verify_command.as_deref(),
            Some("cargo check"),
            "payload must carry verify_command from the scheduled task"
        );
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_payload_omits_verify_command_when_none() {
        // When verify_command is None, skip_serializing_if omits it from JSON
        // so the payload stays backward-compatible (no extra key for old
        // Python consumers that don't know about verify_command).
        let task = make_cron_task();
        assert!(task.verify_command.is_none());
        let payload = NatsSubmitDispatcher::build_payload(&task);
        assert!(payload.verify_command.is_none());
        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(
            parsed.get("verify_command").is_none(),
            "verify_command must be absent from JSON when None"
        );
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_payload_matches_existing_publisher() {
        // The payload shape must match what TaskService::submit_task publishes
        // (server.rs:3092) — same NatsTaskSubmit struct, same fields. This is
        // the contract the Python _handle_submit consumer expects.
        let task = make_cron_task();
        let dispatcher_payload = NatsSubmitDispatcher::build_payload(&task);

        // Simulate what TaskService does (server.rs:3092-3096):
        let existing_publisher_payload = NatsTaskSubmit {
            task_id: Uuid::new_v4().to_string(), // TaskId::new()
            description: task.description.clone(),
            project_id: task.project_id.clone(),
            verify_command: None,
        };

        // Both must serialize to the same JSON shape (field names + types).
        let d_json = serde_json::to_value(&dispatcher_payload).unwrap();
        let e_json = serde_json::to_value(&existing_publisher_payload).unwrap();
        assert_eq!(
            d_json.get("task_id").map(|v| v.is_string()),
            e_json.get("task_id").map(|v| v.is_string()),
            "task_id field type must match"
        );
        assert_eq!(
            d_json.get("description").map(|v| v.is_string()),
            e_json.get("description").map(|v| v.is_string()),
            "description field type must match"
        );
        assert_eq!(
            d_json.get("project_id").map(|v| v.is_string()),
            e_json.get("project_id").map(|v| v.is_string()),
            "project_id field type must match"
        );
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_submit_dispatcher_build_failed_history() {
        // Verify the Failed ExecutionHistory construction for publish failures.
        let scheduled_task_id = Uuid::new_v4();
        let started_at = chrono::Utc::now();
        let history = NatsSubmitDispatcher::build_failed_history(
            scheduled_task_id,
            started_at,
            "connection refused",
        );

        assert_eq!(history.scheduled_task_id, scheduled_task_id);
        assert_eq!(history.status, ExecutionStatus::Failed);
        assert!(history.started_at >= started_at);
        assert!(history.completed_at.is_some());
        assert!(
            history
                .result_summary
                .as_ref()
                .map(|s| s.contains("NATS publish failed") && s.contains("connection refused"))
                .unwrap_or(false),
            "Failed summary must mention NATS publish failure + error: {:?}",
            history.result_summary
        );
    }

    #[cfg(feature = "messaging")]
    #[tokio::test]
    async fn nats_submit_dispatcher_records_failed_history_on_publish_error() {
        // Integration: verify that a Failed ExecutionHistory (as built by
        // NatsSubmitDispatcher on publish failure) is correctly recorded by
        // SchedulerService. This mirrors EngineSubmitDispatcher's test at
        // dispatcher.rs:608 (engine_submit_dispatcher_failure_records_failed_history).
        //
        // We can't easily mock async_nats::Client (concrete type, not a trait),
        // so we test the history-recording path directly: build the Failed
        // entry (as dispatch would on publish failure) and verify
        // SchedulerService.record_execution stores it.
        use uc_engine::scheduler::SchedulerService;

        let service = SchedulerService::new();
        let task_id = Uuid::new_v4();

        // Simulate the Completed entry from dispatch_with_guard (spawn succeeded)
        let completed = ExecutionHistory {
            id: Uuid::new_v4(),
            scheduled_task_id: task_id,
            started_at: chrono::Utc::now(),
            completed_at: Some(chrono::Utc::now()),
            status: ExecutionStatus::Completed,
            result_summary: Some("Task dispatched successfully".to_string()),
            deferred_reason: None,
        };
        service.record_execution(&completed).await;

        // Simulate the Failed entry from NatsSubmitDispatcher's spawned task
        // (publish failed asynchronously)
        let failed = NatsSubmitDispatcher::build_failed_history(
            task_id,
            chrono::Utc::now(),
            "connection refused",
        );
        service.record_execution(&failed).await;

        // Both entries should be present — Completed (spawn) + Failed (publish)
        let history = service.get_execution_history(Some(&task_id)).await;
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].status, ExecutionStatus::Completed);
        assert_eq!(history[1].status, ExecutionStatus::Failed);
        assert!(
            history[1]
                .result_summary
                .as_ref()
                .map(|s| s.contains("NATS publish failed"))
                .unwrap_or(false),
            "Failed entry must be from NatsSubmitDispatcher"
        );
    }

    #[cfg(feature = "messaging")]
    #[tokio::test]
    async fn nats_submit_dispatcher_with_scheduler_service_records_completed() {
        // Integration: SchedulerService with NatsSubmitDispatcher as the
        // dispatcher records Completed ExecutionHistory on dispatch_with_guard
        // (no night window → guard passes → dispatch returns Ok → Completed).
        //
        // We can't construct a NatsSubmitDispatcher without a real NATS client,
        // so we verify the dispatch_with_guard → Completed path using a
        // LoggingDispatcher stand-in (same trait). The key assertion: the
        // dispatcher-selection logic in start_scheduler wires the right
        // dispatcher, and dispatch_with_guard records Completed regardless of
        // which dispatcher impl is used (the trait contract).
        use uc_engine::scheduler::{LoggingDispatcher, SchedulerService};

        let dispatcher = Arc::new(LoggingDispatcher) as Arc<dyn ScheduleDispatcher>;
        let service = SchedulerService::with_dispatcher(dispatcher);

        let task = make_cron_task();
        let result = service.add_cron_job(task).await.unwrap();

        // Dispatch (no night window → should complete)
        service
            .dispatch_with_guard(&result.task_id)
            .await
            .expect("dispatch should succeed (no night window)");

        // Verify Completed ExecutionHistory
        let history = service.get_execution_history(Some(&result.task_id)).await;
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].status, ExecutionStatus::Completed);
    }

    #[cfg(not(feature = "messaging"))]
    #[test]
    fn nats_submit_dispatcher_stub_without_messaging() {
        let dispatcher = NatsSubmitDispatcher::new();
        let task = make_cron_task();
        let result = dispatcher.dispatch(&task);
        assert!(
            result.is_err(),
            "stub should error without messaging feature"
        );
    }

    // ── NatsKvLockProvider tests ──────────────────────────────────

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_kv_lock_provider_build_lock_key_format() {
        // Verify the lock key format matches what SchedulerService constructs:
        // scheduler:{task_id}:{tick_timestamp}
        let key = NatsKvLockProvider::build_lock_key("abc-123", 1722873600);
        assert_eq!(key, "scheduler:abc-123:1722873600");
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_kv_lock_provider_build_lock_key_deterministic() {
        // The same (task_id, tick_timestamp) must produce the same key on all
        // instances — this is the core determinism requirement.
        let k1 = NatsKvLockProvider::build_lock_key("task-1", 1722873600);
        let k2 = NatsKvLockProvider::build_lock_key("task-1", 1722873600);
        assert_eq!(k1, k2, "same inputs must produce same key");

        // Different tick → different key
        let k3 = NatsKvLockProvider::build_lock_key("task-1", 1722873601);
        assert_ne!(k1, k3, "different tick must produce different key");

        // Different task → different key
        let k4 = NatsKvLockProvider::build_lock_key("task-2", 1722873600);
        assert_ne!(k1, k4, "different task must produce different key");
    }

    #[cfg(feature = "messaging")]
    #[test]
    fn nats_kv_lock_provider_with_random_instance_id() {
        // We can't construct a NatsKvLockProvider without a real NATS client
        // (async_nats::Client is concrete, not mockable). But we can verify
        // the build_lock_key logic and the bucket name constant.
        assert_eq!(SCHEDULER_LOCKS_BUCKET, "scheduler-locks");
    }

    #[cfg(feature = "messaging")]
    #[tokio::test]
    async fn nats_kv_lock_provider_scheduler_service_integration() {
        // Integration: verify that NatsKvLockProvider implements LockProvider
        // correctly and can be injected into SchedulerService via
        // set_lock_provider. We can't construct a NatsKvLockProvider without
        // a real NATS client, so we verify the trait contract with a mock
        // provider (same pattern as the uc-engine tests).
        use uc_engine::scheduler::{LockProvider, NoOpLockProvider, SchedulerService};

        let service = SchedulerService::new();

        // Verify NoOpLockProvider (the default) can be explicitly set
        let noop = Arc::new(NoOpLockProvider) as Arc<dyn LockProvider>;
        service.set_lock_provider(noop).await;

        // Verify a custom blocking provider can be set
        struct BlockingLock;
        impl LockProvider for BlockingLock {
            fn try_acquire(&self, _key: &str, _ttl: std::time::Duration) -> bool {
                false
            }
        }
        let blocking = Arc::new(BlockingLock) as Arc<dyn LockProvider>;
        service.set_lock_provider(blocking).await;

        // The set_lock_provider call should not panic and the service should
        // remain usable. The actual lock-skip behavior is tested in uc-engine
        // (cron_callback_skips_on_lock_failure).
    }

    #[cfg(not(feature = "messaging"))]
    #[test]
    fn nats_kv_lock_provider_stub_without_messaging() {
        let _provider = NatsKvLockProvider::new();
        // Without messaging, NatsKvLockProvider is a stub — use NoOpLockProvider
        // instead in real code.
    }
}
