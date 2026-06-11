//! Event sourcing types and EventStore trait.
//!
//! All agent actions are recorded as events for audit, replay, and recovery.
//! The `EventStore` trait abstracts the storage backend (NATS JetStream or
//! in-memory for testing).

use dashmap::DashMap;
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use uc_types::{EngineError, TaskId, WorkerId};

/// A line range within a file (inclusive start, exclusive end).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LineRange {
    pub start: u32,
    pub end: u32,
}

impl LineRange {
    pub fn new(start: u32, end: u32) -> Self {
        Self { start, end }
    }

    /// Check if two line ranges overlap.
    pub fn overlaps(&self, other: &LineRange) -> bool {
        self.start < other.end && other.start < self.end
    }
}

/// Agent event types for the event sourcing system.
///
/// Every significant action in the agent system is recorded as an event.
/// Events are appended to the event store and can be replayed for recovery.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AgentEventType {
    TaskCreated {
        task_id: TaskId,
        description: String,
    },
    SubtaskAssigned {
        subtask_id: TaskId,
        worker_id: WorkerId,
    },
    SubtaskStarted {
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
        success: bool,
    },
    FileModified {
        subtask_id: TaskId,
        file_path: String,
        diff: String,
    },
    EditIntent {
        worker_id: WorkerId,
        file_path: String,
        edit_type: String,
        regions: Vec<LineRange>,
    },
    SubtaskCompleted {
        subtask_id: TaskId,
        summary: String,
        success: bool,
    },
    SubtaskFailed {
        subtask_id: TaskId,
        error: String,
        recoverable: bool,
    },
    CheckpointCreated {
        task_id: TaskId,
        snapshot_id: String,
        event_offset: u64,
    },
}

/// A recorded event with metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecordedEvent {
    /// Monotonically increasing offset within the stream.
    pub offset: u64,
    /// Unix timestamp (milliseconds).
    pub timestamp: i64,
    /// The event payload.
    pub event: AgentEventType,
    /// Optional subject/stream this event belongs to.
    pub subject: String,
}

/// Snapshot of a task's state for checkpoint/recovery.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskSnapshot {
    pub task_id: String,
    pub status: String,
    pub subtasks: Vec<SubtaskSnapshot>,
    pub last_event_offset: u64,
    /// Unix timestamp (milliseconds).
    pub timestamp: i64,
}

/// Snapshot of a single subtask's state.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubtaskSnapshot {
    pub subtask_id: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub result_summary: Option<String>,
}

/// Trait for event storage backends.
///
/// Implementations:
/// - `InMemoryEventStore`: always available, for testing
/// - `NatsEventStore`: feature-gated on `messaging`, uses NATS JetStream
#[async_trait::async_trait]
pub trait EventStore: Send + Sync {
    /// Append an event to the store. Returns the assigned offset.
    async fn append(&self, subject: &str, event: &AgentEventType) -> Result<u64, EngineError>;

    /// Read events from the given offset (inclusive) for a subject.
    async fn read_from(
        &self,
        subject: &str,
        offset: u64,
    ) -> Result<Vec<RecordedEvent>, EngineError>;

    /// Get the latest offset for a subject. Returns 0 if no events exist.
    async fn latest_offset(&self, subject: &str) -> Result<u64, EngineError>;
}

/// In-memory event store for testing and fallback.
pub struct InMemoryEventStore {
    streams: DashMap<String, Vec<RecordedEvent>>,
    global_offset: AtomicU64,
}

impl InMemoryEventStore {
    pub fn new() -> Self {
        Self {
            streams: DashMap::new(),
            global_offset: AtomicU64::new(1),
        }
    }
}

impl Default for InMemoryEventStore {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait::async_trait]
impl EventStore for InMemoryEventStore {
    async fn append(&self, subject: &str, event: &AgentEventType) -> Result<u64, EngineError> {
        let offset = self.global_offset.fetch_add(1, Ordering::SeqCst);
        let timestamp = chrono::Utc::now().timestamp_millis();

        let recorded = RecordedEvent {
            offset,
            timestamp,
            event: event.clone(),
            subject: subject.to_string(),
        };

        self.streams
            .entry(subject.to_string())
            .or_default()
            .push(recorded);

        Ok(offset)
    }

    async fn read_from(
        &self,
        subject: &str,
        offset: u64,
    ) -> Result<Vec<RecordedEvent>, EngineError> {
        let stream = self.streams.get(subject);
        match stream {
            Some(events) => Ok(events
                .iter()
                .filter(|e| e.offset >= offset)
                .cloned()
                .collect()),
            None => Ok(Vec::new()),
        }
    }

    async fn latest_offset(&self, subject: &str) -> Result<u64, EngineError> {
        let stream = self.streams.get(subject);
        match stream {
            Some(events) => Ok(events.last().map(|e| e.offset).unwrap_or(0)),
            None => Ok(0),
        }
    }
}

/// NATS JetStream-backed event store.
///
/// Feature-gated on `messaging`. Provides durable event storage with
/// replay capabilities for production deployments.
#[cfg(feature = "messaging")]
pub struct NatsEventStore {
    #[allow(dead_code)]
    client: async_nats::Client,
    jetstream: async_nats::jetstream::Context,
}

#[cfg(feature = "messaging")]
impl NatsEventStore {
    /// Create a new NATS event store.
    ///
    /// Connects to the NATS server and ensures the required stream exists.
    pub async fn new(nats_url: &str) -> Result<Self, EngineError> {
        let client = async_nats::connect(nats_url)
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS connect failed: {}", e)))?;

        let jetstream = async_nats::jetstream::new(client.clone());

        // Ensure the agent events stream exists
        jetstream
            .create_stream(async_nats::jetstream::stream::Config {
                name: "AGENT_EVENTS".to_string(),
                subjects: vec!["agent.events.>".to_string()],
                ..Default::default()
            })
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("NATS stream create failed: {}", e))
            })?;

        Ok(Self { client, jetstream })
    }
}

#[cfg(feature = "messaging")]
#[async_trait::async_trait]
impl EventStore for NatsEventStore {
    async fn append(&self, subject: &str, event: &AgentEventType) -> Result<u64, EngineError> {
        let payload = serde_json::to_vec(event).map_err(|e| {
            EngineError::InternalError(format!("Event serialization failed: {}", e))
        })?;

        let subject_owned = subject.to_string();
        let ack = self
            .jetstream
            .publish(subject_owned, payload.into())
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS publish failed: {}", e)))?;

        let sequence = ack
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS ack failed: {}", e)))?
            .sequence;

        Ok(sequence)
    }

    async fn read_from(
        &self,
        subject: &str,
        offset: u64,
    ) -> Result<Vec<RecordedEvent>, EngineError> {
        let stream = self
            .jetstream
            .get_stream("AGENT_EVENTS")
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS get stream failed: {}", e)))?;

        let consumer = stream
            .create_consumer(async_nats::jetstream::consumer::pull::Config {
                filter_subject: subject.to_string(),
                deliver_policy: async_nats::jetstream::consumer::DeliverPolicy::ByStartSequence {
                    start_sequence: offset,
                },
                ..Default::default()
            })
            .await
            .map_err(|e| {
                EngineError::ConnectionError(format!("NATS consumer create failed: {}", e))
            })?;

        let mut results = Vec::new();
        let mut messages = consumer.messages().await.map_err(|e| {
            EngineError::ConnectionError(format!("NATS message stream failed: {}", e))
        })?;

        // Read up to 1000 messages or until no more are available
        use futures::StreamExt;
        for _ in 0..1000 {
            match tokio::time::timeout(std::time::Duration::from_millis(100), messages.next()).await
            {
                Ok(Some(Ok(message))) => {
                    let event: AgentEventType = serde_json::from_slice(&message.payload)
                        .unwrap_or_else(|_| AgentEventType::TaskCreated {
                            task_id: TaskId::new(),
                            description: String::new(),
                        });

                    let sequence = message.info().map(|i| i.stream_sequence).unwrap_or(0);

                    results.push(RecordedEvent {
                        offset: sequence,
                        timestamp: chrono::Utc::now().timestamp_millis(),
                        event,
                        subject: subject.to_string(),
                    });
                }
                Ok(Some(Err(_))) | Ok(None) => break,
                Err(_) => break, // Timeout, no more messages available right now
            }
        }

        Ok(results)
    }

    async fn latest_offset(&self, _subject: &str) -> Result<u64, EngineError> {
        let mut stream = self
            .jetstream
            .get_stream("AGENT_EVENTS")
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS get stream failed: {}", e)))?;

        let info = stream
            .info()
            .await
            .map_err(|e| EngineError::ConnectionError(format!("NATS stream info failed: {}", e)))?;

        Ok(info.state.last_sequence)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::{TaskId, WorkerId};

    #[tokio::test]
    async fn in_memory_event_store_append_and_read() {
        let store = InMemoryEventStore::new();
        let task_id = TaskId::new();
        let worker_id = WorkerId::new();

        // Append events
        let offset1 = store
            .append(
                "agent.events.task1",
                &AgentEventType::TaskCreated {
                    task_id: task_id.clone(),
                    description: "Test task".to_string(),
                },
            )
            .await
            .unwrap();

        let offset2 = store
            .append(
                "agent.events.task1",
                &AgentEventType::SubtaskAssigned {
                    subtask_id: TaskId::new(),
                    worker_id: worker_id.clone(),
                },
            )
            .await
            .unwrap();

        assert!(offset2 > offset1);

        // Read all events
        let events = store.read_from("agent.events.task1", 0).await.unwrap();
        assert_eq!(events.len(), 2);

        // Read from offset (should only get the second event)
        let events = store
            .read_from("agent.events.task1", offset2)
            .await
            .unwrap();
        assert_eq!(events.len(), 1);
    }

    #[tokio::test]
    async fn in_memory_event_store_latest_offset() {
        let store = InMemoryEventStore::new();

        // No events yet
        let offset = store.latest_offset("agent.events.task1").await.unwrap();
        assert_eq!(offset, 0);

        // After appending
        store
            .append(
                "agent.events.task1",
                &AgentEventType::TaskCreated {
                    task_id: TaskId::new(),
                    description: "Test".to_string(),
                },
            )
            .await
            .unwrap();

        let offset = store.latest_offset("agent.events.task1").await.unwrap();
        assert!(offset > 0);
    }

    #[tokio::test]
    async fn in_memory_event_store_separate_subjects() {
        let store = InMemoryEventStore::new();

        store
            .append(
                "agent.events.task1",
                &AgentEventType::TaskCreated {
                    task_id: TaskId::new(),
                    description: "Task 1".to_string(),
                },
            )
            .await
            .unwrap();

        store
            .append(
                "agent.events.task2",
                &AgentEventType::TaskCreated {
                    task_id: TaskId::new(),
                    description: "Task 2".to_string(),
                },
            )
            .await
            .unwrap();

        let events1 = store.read_from("agent.events.task1", 0).await.unwrap();
        let events2 = store.read_from("agent.events.task2", 0).await.unwrap();

        assert_eq!(events1.len(), 1);
        assert_eq!(events2.len(), 1);
    }

    #[test]
    fn line_range_overlaps() {
        let r1 = LineRange::new(1, 10);
        let r2 = LineRange::new(5, 15);
        let r3 = LineRange::new(10, 20);
        let r4 = LineRange::new(20, 30);

        assert!(r1.overlaps(&r2)); // overlapping
        assert!(!r1.overlaps(&r3)); // touching but not overlapping (end is exclusive)
        assert!(!r1.overlaps(&r4)); // completely separate
    }

    #[test]
    fn event_type_serialization() {
        let event = AgentEventType::TaskCreated {
            task_id: TaskId::new(),
            description: "Test task".to_string(),
        };

        let json = serde_json::to_string(&event).unwrap();
        let deserialized: AgentEventType = serde_json::from_str(&json).unwrap();

        match deserialized {
            AgentEventType::TaskCreated { description, .. } => {
                assert_eq!(description, "Test task");
            }
            _ => panic!("Expected TaskCreated variant"),
        }
    }
}
