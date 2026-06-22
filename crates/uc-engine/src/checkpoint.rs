//! Checkpoint manager for Event Sourcing with snapshot optimization.
//!
//! Events are appended to the event store for durability.
//! Periodic snapshots are stored in the memory store.
//! Recovery loads the latest snapshot and replays subsequent events.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use dashmap::DashMap;

use uc_types::EngineError;

use crate::events::{AgentEventType, EventStore, RecordedEvent, SubtaskSnapshot, TaskSnapshot};

/// Configuration for the checkpoint manager.
#[derive(Debug, Clone)]
pub struct CheckpointConfig {
    /// Number of events between automatic snapshots.
    pub snapshot_interval: u64,
    /// Memory key prefix for snapshots.
    pub snapshot_prefix: String,
}

impl Default for CheckpointConfig {
    fn default() -> Self {
        Self {
            snapshot_interval: 100,
            snapshot_prefix: "snapshot".to_string(),
        }
    }
}

/// Manages event sourcing with periodic snapshots for efficient recovery.
///
/// # Recovery Flow
/// 1. Load latest snapshot from memory store
/// 2. Replay events after the snapshot's `last_event_offset`
/// 3. Apply events to reconstruct current state
///
/// # Snapshot Trigger
/// Snapshots are created automatically every `snapshot_interval` events,
/// or manually via `create_snapshot()`.
pub struct CheckpointManager {
    event_store: Arc<dyn EventStore>,
    /// In-memory fallback for snapshots when no external memory store is available.
    snapshot_store: DashMap<String, TaskSnapshot>,
    config: CheckpointConfig,
    event_count: AtomicU64,
}

impl CheckpointManager {
    /// Create a new checkpoint manager with an event store backend.
    pub fn new(event_store: Arc<dyn EventStore>, config: CheckpointConfig) -> Self {
        Self {
            event_store,
            snapshot_store: DashMap::new(),
            config,
            event_count: AtomicU64::new(0),
        }
    }

    /// Record an event to the event store.
    ///
    /// If the event count reaches the snapshot interval, a snapshot is
    /// automatically created for the relevant task.
    pub async fn record_event(
        &self,
        subject: &str,
        event: AgentEventType,
    ) -> Result<u64, EngineError> {
        let offset = self.event_store.append(subject, &event).await?;

        let count = self.event_count.fetch_add(1, Ordering::SeqCst) + 1;

        // Auto-snapshot at configured interval
        if count.is_multiple_of(self.config.snapshot_interval) {
            if let Some(task_id) = extract_task_id(&event) {
                // Best-effort snapshot; log but don't fail the event recording
                if let Err(e) = self.create_snapshot(&task_id).await {
                    tracing::warn!("Auto-snapshot failed for task {}: {}", task_id, e);
                }
            }
        }

        Ok(offset)
    }

    /// Create a snapshot of the current task state.
    ///
    /// The snapshot captures the current state of all subtasks and the
    /// last event offset. It is stored in the memory store for efficient
    /// recovery.
    pub async fn create_snapshot(&self, task_id: &str) -> Result<String, EngineError> {
        let subject = format!("agent.events.{}", task_id);
        let latest_offset = self.event_store.latest_offset(&subject).await?;

        let snapshot_id = format!(
            "{}:{}:{}",
            self.config.snapshot_prefix, task_id, latest_offset
        );

        // Collect subtask states by replaying events
        let subtasks = self.collect_subtask_states(task_id).await?;

        let timestamp = chrono::Utc::now().timestamp_millis();

        let snapshot = TaskSnapshot {
            task_id: task_id.to_string(),
            status: self.determine_task_status(&subtasks),
            subtasks,
            last_event_offset: latest_offset,
            timestamp,
        };

        // Store snapshot in the DashMap (in production, this would go to TiKV)
        self.snapshot_store
            .insert(snapshot_id.clone(), snapshot.clone());

        // Also record the checkpoint event
        let checkpoint_event = AgentEventType::CheckpointCreated {
            task_id: uc_types::TaskId(task_id.to_string()),
            snapshot_id: snapshot_id.clone(),
            event_offset: latest_offset,
        };
        self.event_store.append(&subject, &checkpoint_event).await?;

        tracing::info!(
            "Created snapshot {} for task {} at offset {}",
            snapshot_id,
            task_id,
            latest_offset
        );

        Ok(snapshot_id)
    }

    /// Recover task state from the latest snapshot + event replay.
    ///
    /// 1. Find the latest snapshot for the task
    /// 2. Load the snapshot
    /// 3. Replay events after the snapshot's offset
    /// 4. Return the reconstructed state
    pub async fn recover(&self, task_id: &str) -> Result<TaskSnapshot, EngineError> {
        // Find latest snapshot
        let snapshot = self.find_latest_snapshot(task_id).await?;

        match snapshot {
            Some(snap) => {
                let from_offset = snap.last_event_offset + 1;
                let subject = format!("agent.events.{}", task_id);

                // Replay events after snapshot
                let events = self.event_store.read_from(&subject, from_offset).await?;

                let mut state = snap;
                for event in &events {
                    apply_event_to_snapshot(&mut state, &event.event);
                }

                state.last_event_offset = events
                    .last()
                    .map(|e| e.offset)
                    .unwrap_or(state.last_event_offset);

                tracing::info!(
                    "Recovered task {} from snapshot, replayed {} events",
                    task_id,
                    events.len()
                );

                Ok(state)
            }
            None => {
                // No snapshot exists, replay from the beginning
                let subject = format!("agent.events.{}", task_id);
                let events = self.event_store.read_from(&subject, 0).await?;

                let mut state = TaskSnapshot {
                    task_id: task_id.to_string(),
                    status: "created".to_string(),
                    subtasks: Vec::new(),
                    last_event_offset: 0,
                    timestamp: chrono::Utc::now().timestamp_millis(),
                };

                for event in &events {
                    apply_event_to_snapshot(&mut state, &event.event);
                }

                state.last_event_offset = events.last().map(|e| e.offset).unwrap_or(0);

                tracing::info!(
                    "Recovered task {} from scratch, replayed {} events",
                    task_id,
                    events.len()
                );

                Ok(state)
            }
        }
    }

    /// List all events for a task from a given offset.
    pub async fn list_events(
        &self,
        task_id: &str,
        from_offset: u64,
    ) -> Result<Vec<RecordedEvent>, EngineError> {
        let subject = format!("agent.events.{}", task_id);
        self.event_store.read_from(&subject, from_offset).await
    }

    /// Collect subtask states by replaying events for a task.
    async fn collect_subtask_states(
        &self,
        task_id: &str,
    ) -> Result<Vec<SubtaskSnapshot>, EngineError> {
        let subject = format!("agent.events.{}", task_id);
        let events = self.event_store.read_from(&subject, 0).await?;

        let mut subtasks: Vec<SubtaskSnapshot> = Vec::new();
        for event in &events {
            match &event.event {
                AgentEventType::SubtaskAssigned {
                    task_id: _,
                    subtask_id,
                    worker_id,
                } => {
                    subtasks.push(SubtaskSnapshot {
                        subtask_id: subtask_id.0.clone(),
                        status: "assigned".to_string(),
                        assigned_worker: Some(worker_id.0.clone()),
                        result_summary: None,
                    });
                }
                AgentEventType::SubtaskStarted {
                    task_id: _,
                    subtask_id,
                    ..
                } => {
                    if let Some(st) = subtasks.iter_mut().find(|s| s.subtask_id == subtask_id.0) {
                        st.status = "in_progress".to_string();
                    }
                }
                AgentEventType::SubtaskCompleted {
                    task_id: _,
                    subtask_id,
                    summary,
                    success,
                    result: _,
                } => {
                    if let Some(st) = subtasks.iter_mut().find(|s| s.subtask_id == subtask_id.0) {
                        st.status = if *success { "completed" } else { "failed" }.to_string();
                        st.result_summary = Some(summary.clone());
                    }
                }
                AgentEventType::SubtaskFailed {
                    task_id: _,
                    subtask_id,
                    error,
                    recoverable,
                } => {
                    if let Some(st) = subtasks.iter_mut().find(|s| s.subtask_id == subtask_id.0) {
                        st.status = if *recoverable {
                            "recoverable_failed"
                        } else {
                            "failed"
                        }
                        .to_string();
                        st.result_summary = Some(error.clone());
                    }
                }
                _ => {}
            }
        }

        Ok(subtasks)
    }

    /// Determine overall task status from subtask states.
    fn determine_task_status(&self, subtasks: &[SubtaskSnapshot]) -> String {
        if subtasks.is_empty() {
            return "created".to_string();
        }

        let all_completed = subtasks.iter().all(|s| s.status == "completed");
        let any_failed = subtasks.iter().any(|s| s.status == "failed");
        let any_in_progress = subtasks
            .iter()
            .any(|s| s.status == "in_progress" || s.status == "assigned");

        if all_completed {
            "completed".to_string()
        } else if any_failed {
            "failed".to_string()
        } else if any_in_progress {
            "in_progress".to_string()
        } else {
            "pending".to_string()
        }
    }

    /// Find the latest snapshot for a task from the in-memory store.
    async fn find_latest_snapshot(
        &self,
        task_id: &str,
    ) -> Result<Option<TaskSnapshot>, EngineError> {
        let prefix = format!("{}:{}:", self.config.snapshot_prefix, task_id);

        let mut latest: Option<TaskSnapshot> = None;
        let mut latest_offset: u64 = 0;

        for entry in self.snapshot_store.iter() {
            if entry.key().starts_with(&prefix) && entry.value().last_event_offset >= latest_offset
            {
                latest_offset = entry.value().last_event_offset;
                latest = Some(entry.value().clone());
            }
        }

        Ok(latest)
    }
}

/// Extract task_id from an event for auto-snapshotting.
fn extract_task_id(event: &AgentEventType) -> Option<String> {
    match event {
        AgentEventType::TaskCreated { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::SubtaskAssigned { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::SubtaskStarted { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::ToolInvoked { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::ToolResult { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::FileModified { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::EditIntent { .. } => None,
        AgentEventType::SubtaskCompleted { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::SubtaskFailed { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::CheckpointCreated { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::TaskCompleted { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::TaskFailed { task_id, .. } => Some(task_id.0.clone()),
        AgentEventType::TaskPaused { task_id } => Some(task_id.0.clone()),
        AgentEventType::TaskResumed { task_id } => Some(task_id.0.clone()),
    }
}

/// Apply an event to a task snapshot (for replay).
fn apply_event_to_snapshot(snapshot: &mut TaskSnapshot, event: &AgentEventType) {
    match event {
        AgentEventType::TaskCreated { .. } => {
            snapshot.status = "created".to_string();
        }
        AgentEventType::SubtaskAssigned {
            task_id: _,
            subtask_id,
            worker_id,
        } => {
            let sid = subtask_id.0.clone();
            if !snapshot.subtasks.iter().any(|s| s.subtask_id == sid) {
                snapshot.subtasks.push(SubtaskSnapshot {
                    subtask_id: sid,
                    status: "assigned".to_string(),
                    assigned_worker: Some(worker_id.0.clone()),
                    result_summary: None,
                });
            }
            snapshot.status = "in_progress".to_string();
        }
        AgentEventType::SubtaskStarted {
            task_id: _,
            subtask_id,
            ..
        } => {
            if let Some(st) = snapshot
                .subtasks
                .iter_mut()
                .find(|s| s.subtask_id == subtask_id.0)
            {
                st.status = "in_progress".to_string();
            }
        }
        AgentEventType::SubtaskCompleted {
            task_id: _,
            subtask_id,
            summary,
            success,
            result: _,
        } => {
            if let Some(st) = snapshot
                .subtasks
                .iter_mut()
                .find(|s| s.subtask_id == subtask_id.0)
            {
                st.status = if *success { "completed" } else { "failed" }.to_string();
                st.result_summary = Some(summary.clone());
            }
        }
        AgentEventType::SubtaskFailed {
            task_id: _,
            subtask_id,
            error,
            recoverable,
        } => {
            if let Some(st) = snapshot
                .subtasks
                .iter_mut()
                .find(|s| s.subtask_id == subtask_id.0)
            {
                st.status = if *recoverable {
                    "recoverable_failed"
                } else {
                    "failed"
                }
                .to_string();
                st.result_summary = Some(error.clone());
            }
        }
        AgentEventType::TaskPaused { .. } => {
            snapshot.status = "paused".to_string();
        }
        AgentEventType::TaskResumed { .. } => {
            snapshot.status = "in_progress".to_string();
        }
        AgentEventType::ToolInvoked { .. }
        | AgentEventType::ToolResult { .. }
        | AgentEventType::FileModified { .. }
        | AgentEventType::EditIntent { .. }
        | AgentEventType::CheckpointCreated { .. }
        | AgentEventType::TaskCompleted { .. }
        | AgentEventType::TaskFailed { .. } => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::InMemoryEventStore;
    use uc_types::{TaskId, WorkerId};

    #[tokio::test]
    async fn record_event_returns_increasing_offsets() {
        let store = Arc::new(InMemoryEventStore::new());
        let manager = CheckpointManager::new(store, CheckpointConfig::default());

        let task_id = TaskId::new();
        let offset1 = manager
            .record_event(
                "agent.events.task1",
                AgentEventType::TaskCreated {
                    task_id: task_id.clone(),
                    description: "Test".to_string(),
                },
            )
            .await
            .unwrap();

        let offset2 = manager
            .record_event(
                "agent.events.task1",
                AgentEventType::SubtaskAssigned {
                    task_id: TaskId("task1".to_string()),
                    subtask_id: TaskId::new(),
                    worker_id: WorkerId::new(),
                },
            )
            .await
            .unwrap();

        assert!(offset2 > offset1);
    }

    #[tokio::test]
    async fn create_snapshot_and_recover() {
        let store = Arc::new(InMemoryEventStore::new());
        let config = CheckpointConfig {
            snapshot_interval: 100,
            snapshot_prefix: "snapshot".to_string(),
        };
        let manager = CheckpointManager::new(store, config);

        let task_id = "test-task-1";
        let subtask_id = TaskId::new();
        let worker_id = WorkerId::new();

        // Record events
        manager
            .record_event(
                "agent.events.test-task-1",
                AgentEventType::TaskCreated {
                    task_id: TaskId(task_id.to_string()),
                    description: "Test task".to_string(),
                },
            )
            .await
            .unwrap();

        manager
            .record_event(
                "agent.events.test-task-1",
                AgentEventType::SubtaskAssigned {
                    task_id: TaskId(task_id.to_string()),
                    subtask_id: subtask_id.clone(),
                    worker_id: worker_id.clone(),
                },
            )
            .await
            .unwrap();

        // Create snapshot
        let snapshot_id = manager.create_snapshot(task_id).await.unwrap();
        assert!(!snapshot_id.is_empty());

        // Record more events after snapshot
        manager
            .record_event(
                "agent.events.test-task-1",
                AgentEventType::SubtaskCompleted {
                    task_id: TaskId(task_id.to_string()),
                    subtask_id: subtask_id.clone(),
                    summary: "Done".to_string(),
                    success: true,
                    result: String::new(),
                },
            )
            .await
            .unwrap();

        // Recover should replay the post-snapshot event
        let state = manager.recover(task_id).await.unwrap();
        assert_eq!(state.task_id, task_id);
        assert_eq!(state.subtasks.len(), 1);
        assert_eq!(state.subtasks[0].status, "completed");
    }

    #[tokio::test]
    async fn recover_from_scratch_no_snapshot() {
        let store = Arc::new(InMemoryEventStore::new());
        let manager = CheckpointManager::new(store, CheckpointConfig::default());

        let task_id = "test-task-2";

        // Record events without snapshot
        manager
            .record_event(
                "agent.events.test-task-2",
                AgentEventType::TaskCreated {
                    task_id: TaskId(task_id.to_string()),
                    description: "No snapshot test".to_string(),
                },
            )
            .await
            .unwrap();

        // Recover should replay from beginning
        let state = manager.recover(task_id).await.unwrap();
        assert_eq!(state.task_id, task_id);
        assert_eq!(state.status, "created");
    }

    #[tokio::test]
    async fn auto_snapshot_at_interval() {
        let store = Arc::new(InMemoryEventStore::new());
        let config = CheckpointConfig {
            snapshot_interval: 3, // Snapshot every 3 events
            snapshot_prefix: "snap".to_string(),
        };
        let manager = CheckpointManager::new(store, config);

        let task_id = TaskId::new();

        // Record 3 events (should trigger auto-snapshot)
        for i in 0..3 {
            manager
                .record_event(
                    &format!("agent.events.{}", task_id.0),
                    AgentEventType::TaskCreated {
                        task_id: task_id.clone(),
                        description: format!("Event {}", i),
                    },
                )
                .await
                .unwrap();
        }

        // The snapshot store should have an entry
        assert!(!manager.snapshot_store.is_empty());
    }

    #[tokio::test]
    async fn list_events_returns_all_events() {
        let store = Arc::new(InMemoryEventStore::new());
        let manager = CheckpointManager::new(store, CheckpointConfig::default());

        let task_id = "test-task-list";

        manager
            .record_event(
                "agent.events.test-task-list",
                AgentEventType::TaskCreated {
                    task_id: TaskId(task_id.to_string()),
                    description: "List test".to_string(),
                },
            )
            .await
            .unwrap();

        manager
            .record_event(
                "agent.events.test-task-list",
                AgentEventType::SubtaskAssigned {
                    task_id: TaskId(task_id.to_string()),
                    subtask_id: TaskId::new(),
                    worker_id: WorkerId::new(),
                },
            )
            .await
            .unwrap();

        let events = manager.list_events(task_id, 0).await.unwrap();
        assert_eq!(events.len(), 2);
    }
}
