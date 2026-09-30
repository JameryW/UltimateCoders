#![cfg(feature = "messaging")]

use std::sync::Arc;
use uc_engine::checkpoint::{CheckpointConfig, CheckpointManager};
use uc_engine::{AgentEventType, EventStore, NatsEventStore};
use uc_types::{TaskId, WorkerId};

#[tokio::test]
#[ignore = "requires an isolated NATS broker via UC_NATS_TEST_URL"]
async fn replay_from_zero_and_checkpoints_preserve_parent_control_state() {
    let url = std::env::var("UC_NATS_TEST_URL").expect("set UC_NATS_TEST_URL");
    let store = Arc::new(NatsEventStore::new(&url).await.unwrap());
    let id = format!("event-replay-{}", uuid::Uuid::new_v4());
    let subject = format!("task.{id}");
    let created = store
        .append(
            &subject,
            &AgentEventType::TaskCreated {
                task_id: TaskId(id.clone()),
                description: "Replay fixture".into(),
            },
        )
        .await
        .unwrap();
    let assigned = store
        .append(
            &subject,
            &AgentEventType::SubtaskAssigned {
                task_id: TaskId(id.clone()),
                subtask_id: TaskId(format!("{id}-node")),
                worker_id: WorkerId("fixture-worker".into()),
            },
        )
        .await
        .unwrap();
    store
        .append(
            &subject,
            &AgentEventType::TaskPaused {
                task_id: TaskId(id.clone()),
            },
        )
        .await
        .unwrap();
    let all = store.read_from(&subject, 0).await.unwrap();
    assert_eq!(all.len(), 3);
    assert_eq!(all[0].offset, created);
    assert_eq!(store.read_from(&subject, assigned).await.unwrap().len(), 2);
    // Soft pause lets an already-running child finish, but must keep its
    // parent paused until an explicit resume or parent terminal event.
    store
        .append(
            &subject,
            &AgentEventType::SubtaskCompleted {
                task_id: TaskId(id.clone()),
                subtask_id: TaskId(format!("{id}-node")),
                summary: "finished while paused".into(),
                success: true,
                modified_files: Vec::new(),
                output: String::new(),
                simulated: false,
            },
        )
        .await
        .unwrap();
    store
        .append(
            &subject,
            &AgentEventType::TaskUpdated {
                task_id: TaskId(id.clone()),
                status: "stale_result_rejected".into(),
            },
        )
        .await
        .unwrap();
    let manager = CheckpointManager::new(
        store.clone(),
        CheckpointConfig {
            subject_prefix: "task.".into(),
            ..Default::default()
        },
    );
    manager.create_snapshot(&id).await.unwrap();
    assert_eq!(manager.recover(&id).await.unwrap().status, "paused");
    store
        .append(
            &subject,
            &AgentEventType::TaskResumed {
                task_id: TaskId(id.clone()),
            },
        )
        .await
        .unwrap();
    assert_eq!(manager.recover(&id).await.unwrap().status, "in_progress");
    store
        .append(
            &subject,
            &AgentEventType::TaskCancelled {
                task_id: TaskId(id.clone()),
            },
        )
        .await
        .unwrap();
    manager.create_snapshot(&id).await.unwrap();
    assert_eq!(manager.recover(&id).await.unwrap().status, "failed");
}

#[tokio::test]
#[ignore = "requires an isolated NATS broker via UC_NATS_TEST_URL"]
async fn replay_and_checkpoint_do_not_truncate_long_history() {
    let url = std::env::var("UC_NATS_TEST_URL").expect("set UC_NATS_TEST_URL");
    let store = Arc::new(NatsEventStore::new(&url).await.unwrap());
    let id = format!("long-replay-{}", uuid::Uuid::new_v4());
    let subject = format!("task.{id}");
    for _ in 0..1200 {
        store
            .append(
                &subject,
                &AgentEventType::TaskUpdated {
                    task_id: TaskId(id.clone()),
                    status: "InProgress".into(),
                },
            )
            .await
            .unwrap();
    }
    store
        .append(
            &subject,
            &AgentEventType::TaskPaused {
                task_id: TaskId(id.clone()),
            },
        )
        .await
        .unwrap();
    assert_eq!(store.read_from(&subject, 0).await.unwrap().len(), 1201);
    let manager = CheckpointManager::new(
        store,
        CheckpointConfig {
            subject_prefix: "task.".into(),
            ..Default::default()
        },
    );
    manager.create_snapshot(&id).await.unwrap();
    assert_eq!(manager.recover(&id).await.unwrap().status, "paused");
}
