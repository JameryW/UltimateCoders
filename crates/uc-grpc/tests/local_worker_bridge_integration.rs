//! Integration test: local worker bridge async streaming + broadcast events.
//!
//! Tests that use a mock Python worker subprocess are marked with `#[ignore]`
//! so they do not run in CI (which may not have Python / maturin built).
//! Run them with: ``cargo test -p uc-grpc -- --ignored``

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use tonic::Request;

use uc_engine::LocalEngine;
use uc_grpc::local_worker::{LocalWorkerBridge, WorkerSubtaskUpdate, WorkerTaskUpdate};
use uc_grpc::server::{
    apply_worker_update_to_store, mark_tasks_failed_on_worker_death, GrpcServer, TaskStore,
};
use uc_grpc::ultimate_coders::task_service_server::TaskService;
use uc_grpc::ultimate_coders::*;

// ── Helpers ───────────────────────────────────────────────────────

/// Spawn the mock Python worker for testing.
///
/// Sets UC_MOCK_MODE=1 and UC_MOCK_DELAY_MS=10 for fast test execution.
/// Returns a ``LocalWorkerBridge`` with the subprocess connected.
async fn spawn_mock_worker() -> LocalWorkerBridge {
    let bridge = LocalWorkerBridge::new();
    bridge
        .ensure_worker_with_env(&[("UC_MOCK_MODE", "1"), ("UC_MOCK_DELAY_MS", "10")])
        .await
        .expect("Failed to spawn mock worker");
    bridge
}

// ── Fallback tests (no Python needed) ─────────────────────────────

/// Test: when Python worker is unavailable, submit_task falls back
/// to newline-split decomposition.
#[tokio::test]
async fn fallback_without_worker() {
    let engine = LocalEngine::new_fallback();
    let server = GrpcServer::new(engine);

    // Submit a task via the TaskService trait method
    let response = <GrpcServer<LocalEngine> as TaskService>::submit_task(
        &server,
        Request::new(SubmitTaskRequest {
            description: "1. First step\n2. Second step\n3. Third step".to_string(),
            project_id: "test-project".to_string(),
        }),
    )
    .await
    .unwrap();

    let inner = response.into_inner();
    assert!(inner.success, "submit_task should succeed via fallback");
    assert!(!inner.task_id.is_empty(), "should have a task_id");
    assert_eq!(inner.status, "InProgress");
    assert_eq!(
        inner.subtask_count, 3,
        "newline-split should produce 3 subtasks"
    );
}

/// Test: local decomposition creates correct number of subtasks.
#[tokio::test]
async fn local_decomposition_single_line() {
    let engine = LocalEngine::new_fallback();
    let server = GrpcServer::new(engine);

    let response = <GrpcServer<LocalEngine> as TaskService>::submit_task(
        &server,
        Request::new(SubmitTaskRequest {
            description: "Single task".to_string(),
            project_id: "".to_string(),
        }),
    )
    .await
    .unwrap();

    let inner = response.into_inner();
    assert!(inner.success);
    assert_eq!(
        inner.subtask_count, 1,
        "single line should produce 1 subtask"
    );
}

/// Test: empty description returns error.
#[tokio::test]
async fn empty_description_returns_error() {
    let engine = LocalEngine::new_fallback();
    let server = GrpcServer::new(engine);

    let response = <GrpcServer<LocalEngine> as TaskService>::submit_task(
        &server,
        Request::new(SubmitTaskRequest {
            description: "".to_string(),
            project_id: "".to_string(),
        }),
    )
    .await
    .unwrap();

    let inner = response.into_inner();
    assert!(!inner.success);
    assert!(inner.error.is_some());
    assert!(
        inner.error.unwrap().to_lowercase().contains("empty"),
        "Error message should mention empty"
    );
}

// ── Broadcast event tests (no Python needed) ──────────────────────

/// Test: apply_worker_update_to_store creates a task and broadcasts events.
#[tokio::test]
async fn apply_worker_update_creates_task_and_broadcasts() {
    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));
    let mut rx = event_tx.subscribe();

    let update = WorkerTaskUpdate {
        task_id: "t-broadcast-test".to_string(),
        description: "Test task".to_string(),
        project_id: "proj-1".to_string(),
        status: "InProgress".to_string(),
        subtasks: vec![WorkerSubtaskUpdate {
            id: "s-1".to_string(),
            description: "Subtask 1".to_string(),
            status: "Assigned".to_string(),
            assigned_worker: Some("worker-1".to_string()),
            depends_on: vec![],
        }],
        result: None,
    };

    apply_worker_update_to_store(&update, &task_store, &event_tx).await;

    // Verify task was created
    let store = task_store.lock().await;
    let task = store.get_task("t-broadcast-test").unwrap();
    assert_eq!(task.description, "Test task");
    assert_eq!(task.subtasks.len(), 1);
    drop(store);

    // Verify broadcast event was sent
    let event = rx.try_recv().unwrap();
    assert_eq!(event.task_id, "t-broadcast-test");
}

/// Test: multiple broadcast subscribers receive the same event.
#[tokio::test]
async fn broadcast_multiple_subscribers() {
    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));

    let mut rx1 = event_tx.subscribe();
    let mut rx2 = event_tx.subscribe();

    let update = WorkerTaskUpdate {
        task_id: "t-multi-sub".to_string(),
        description: "Multi subscriber task".to_string(),
        project_id: "".to_string(),
        status: "InProgress".to_string(),
        subtasks: vec![WorkerSubtaskUpdate {
            id: "s-1".to_string(),
            description: "Do work".to_string(),
            status: "InProgress".to_string(),
            assigned_worker: None,
            depends_on: vec![],
        }],
        result: None,
    };

    apply_worker_update_to_store(&update, &task_store, &event_tx).await;

    // Both subscribers should receive the event
    let event1 = rx1.try_recv().unwrap();
    let event2 = rx2.try_recv().unwrap();
    assert_eq!(event1.task_id, "t-multi-sub");
    assert_eq!(event2.task_id, "t-multi-sub");
    assert_eq!(event1.r#type, event2.r#type);
}

/// Test: mark_tasks_failed_on_worker_death marks InProgress tasks.
#[tokio::test]
async fn worker_death_marks_in_progress_tasks_failed() {
    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));

    // Create an InProgress task
    {
        let mut store = task_store.lock().await;
        store.submit_task("In progress task".to_string(), "p1".to_string());
    }

    mark_tasks_failed_on_worker_death(&task_store, &event_tx).await;

    // Verify task is now Failed
    let store = task_store.lock().await;
    let tasks = store.list_tasks();
    assert_eq!(tasks.len(), 1);
    assert_eq!(
        tasks[0].status,
        uc_types::TaskStatus::Failed,
        "InProgress task should be marked Failed on worker death"
    );
}

/// Test: mark_tasks_failed_on_worker_death skips Completed tasks.
#[tokio::test]
async fn worker_death_skips_completed_tasks() {
    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));

    // Create a Completed task
    {
        let mut store = task_store.lock().await;
        let task = store.submit_task("Completed task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();
        store.set_task_status(&task_id, uc_types::TaskStatus::Completed);
    }

    mark_tasks_failed_on_worker_death(&task_store, &event_tx).await;

    // Verify task is still Completed
    let store = task_store.lock().await;
    let tasks = store.list_tasks();
    assert_eq!(tasks.len(), 1);
    assert_eq!(
        tasks[0].status,
        uc_types::TaskStatus::Completed,
        "Completed task should remain Completed"
    );
}

// ── WorkerTaskUpdate serialization tests ───────────────────────────

/// Test: WorkerTaskUpdate JSON roundtrip.
#[tokio::test]
async fn worker_task_update_roundtrip() {
    let update = WorkerTaskUpdate {
        task_id: "t-roundtrip".to_string(),
        description: "Roundtrip test".to_string(),
        project_id: "proj-1".to_string(),
        status: "in_progress".to_string(),
        subtasks: vec![WorkerSubtaskUpdate {
            id: "s-1".to_string(),
            description: "Write test".to_string(),
            status: "assigned".to_string(),
            assigned_worker: Some("w-1".to_string()),
            depends_on: vec![],
        }],
        result: None,
    };

    let json = serde_json::to_string(&update).unwrap();
    let parsed: WorkerTaskUpdate = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.task_id, "t-roundtrip");
    assert_eq!(parsed.status, "in_progress");
    assert_eq!(parsed.subtasks.len(), 1);
}

/// Test: JSON-RPC protocol messages are single-line.
#[tokio::test]
async fn json_rpc_protocol_format() {
    let request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "submit_task",
        "params": {
            "description": "Fix the bug",
            "project_id": "proj-1"
        }
    });

    let notification = serde_json::json!({
        "jsonrpc": "2.0",
        "method": "task_update",
        "params": {
            "task_id": "t-1",
            "status": "in_progress",
            "subtasks": []
        }
    });

    let shutdown = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "shutdown",
        "params": {}
    });

    // All messages must serialize to single-line JSON
    assert!(!serde_json::to_string(&request).unwrap().contains('\n'));
    assert!(!serde_json::to_string(&notification).unwrap().contains('\n'));
    assert!(!serde_json::to_string(&shutdown).unwrap().contains('\n'));
}

// ── Tests requiring Python (marked #[ignore]) ──────────────────────

/// Test: submit a task via the mock worker bridge and verify events
/// are applied to the TaskStore.
///
/// Requires Python 3.10+ installed. Run with:
/// ``cargo test -p uc-grpc --test local_worker_bridge_integration -- --ignored submit_task_via_bridge_with_events``
#[tokio::test]
#[ignore]
async fn submit_task_via_bridge_with_events() {
    let bridge = spawn_mock_worker().await;
    assert!(bridge.is_available(), "Mock worker should be available");

    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));
    let ts = task_store.clone();
    let tx = event_tx.clone();

    // Start notification reader with 3 closures: apply_fn, on_worker_dead, on_restart
    bridge.start_notification_reader(
        move |update: WorkerTaskUpdate| {
            let ts = ts.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                apply_worker_update_to_store(&update, &ts, &tx).await;
            });
        },
        || {
            tracing::warn!("Mock worker died unexpectedly");
        },
        || {
            tracing::info!("Mock worker restarted");
        },
    );

    // Give the reader a moment to start
    tokio::time::sleep(Duration::from_millis(50)).await;

    // Send submit_task
    bridge
        .send_submit_task("1. Write code\n2. Write tests", "test-project")
        .await
        .expect("send_submit_task should succeed");

    // Wait for the worker to process the task (mock mode with 10ms delays)
    tokio::time::sleep(Duration::from_secs(2)).await;

    // Verify task was created in the store
    let store = task_store.lock().await;
    let tasks = store.list_tasks();
    assert!(!tasks.is_empty(), "TaskStore should have at least one task");

    // Find the task (the worker generates a random task_id)
    let task = &tasks[0];
    assert_eq!(task.description, "1. Write code\n2. Write tests");
    assert_eq!(
        task.status,
        uc_types::TaskStatus::Completed,
        "Task should be completed after mock worker processes it"
    );
    assert_eq!(
        task.subtasks.len(),
        2,
        "Should have 2 subtasks from newline-split"
    );

    // Verify subtask statuses
    for subtask in &task.subtasks {
        assert_eq!(
            subtask.status,
            uc_types::SubtaskStatus::Completed,
            "All subtasks should be Completed"
        );
    }

    // Clean up
    bridge.graceful_shutdown().await;
}

/// Test: WatchTask stream receives broadcast events from the worker.
///
/// Requires Python 3.10+ installed. Run with:
/// ``cargo test -p uc-grpc --test local_worker_bridge_integration -- --ignored watch_task_receives_broadcast_events``
#[tokio::test]
#[ignore]
async fn watch_task_receives_broadcast_events() {
    let bridge = spawn_mock_worker().await;
    assert!(bridge.is_available(), "Mock worker should be available");

    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));
    let mut rx = event_tx.subscribe();
    let ts = task_store.clone();
    let tx = event_tx.clone();

    // Start notification reader
    bridge.start_notification_reader(
        move |update: WorkerTaskUpdate| {
            let ts = ts.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                apply_worker_update_to_store(&update, &ts, &tx).await;
            });
        },
        || {
            tracing::warn!("Mock worker died unexpectedly");
        },
        || {
            tracing::info!("Mock worker restarted");
        },
    );

    tokio::time::sleep(Duration::from_millis(50)).await;

    // Send submit_task
    bridge
        .send_submit_task("Fix the bug", "test-project")
        .await
        .expect("send_submit_task should succeed");

    // Collect broadcast events with a timeout
    let mut received_events = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);

    while tokio::time::Instant::now() < deadline {
        tokio::select! {
            result = rx.recv() => {
                match result {
                    Ok(event) => {
                        received_events.push(event);
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!("Lagged {n} events");
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
            _ = tokio::time::sleep(Duration::from_millis(100)) => {
                // If we've received some events and no new ones come, we're done
                if !received_events.is_empty() {
                    // Give a bit more time to collect remaining events
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    while let Ok(event) = rx.try_recv() {
                        received_events.push(event);
                    }
                    break;
                }
            }
        }
    }

    // We should have received at least one broadcast event
    assert!(
        !received_events.is_empty(),
        "Should have received at least one broadcast event from the worker"
    );

    // At least one event should reference the task
    let task_events: Vec<_> = received_events
        .iter()
        .filter(|e| !e.task_id.is_empty())
        .collect();
    assert!(
        !task_events.is_empty(),
        "Should have at least one event with a task_id"
    );

    // Clean up
    bridge.graceful_shutdown().await;
}

/// Test: when the worker crashes, in-progress tasks are marked as Failed.
///
/// Requires Python 3.10+ installed. Run with:
/// ``cargo test -p uc-grpc --test local_worker_bridge_integration -- --ignored worker_crash_marks_tasks_failed``
#[tokio::test]
#[ignore]
async fn worker_crash_marks_tasks_failed() {
    let bridge = spawn_mock_worker().await;
    assert!(bridge.is_available(), "Mock worker should be available");

    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));

    // Create a task in Planning status (simulating a submitted task)
    {
        let mut store = task_store.lock().await;
        store.submit_task_pending(
            "In-progress task at crash time".to_string(),
            "p1".to_string(),
        );
    }

    // Kill the worker abruptly (simulating a crash)
    bridge.kill().await;
    assert!(
        !bridge.is_available(),
        "Worker should be unavailable after kill"
    );

    // Mark tasks as failed
    mark_tasks_failed_on_worker_death(&task_store, &event_tx).await;

    // Verify the Planning task is now Failed
    let store = task_store.lock().await;
    let tasks = store.list_tasks();
    for task in &tasks {
        assert_eq!(
            task.status,
            uc_types::TaskStatus::Failed,
            "Planning task should be marked Failed after worker death"
        );
    }
}

/// Test: multiple subscribers to the broadcast channel all receive events.
///
/// Requires Python 3.10+ installed. Run with:
/// ``cargo test -p uc-grpc --test local_worker_bridge_integration -- --ignored broadcast_multiple_receivers_integration``
#[tokio::test]
#[ignore]
async fn broadcast_multiple_receivers_integration() {
    let bridge = spawn_mock_worker().await;
    assert!(bridge.is_available());

    let (event_tx, _) = tokio::sync::broadcast::channel::<TaskEvent>(256);
    let task_store = Arc::new(Mutex::new(TaskStore::new()));

    let mut rx1 = event_tx.subscribe();
    let mut rx2 = event_tx.subscribe();
    let mut rx3 = event_tx.subscribe();

    let ts = task_store.clone();
    let tx = event_tx.clone();

    bridge.start_notification_reader(
        move |update: WorkerTaskUpdate| {
            let ts = ts.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                apply_worker_update_to_store(&update, &ts, &tx).await;
            });
        },
        || {},
        || {},
    );

    tokio::time::sleep(Duration::from_millis(50)).await;

    bridge
        .send_submit_task("Do a thing", "")
        .await
        .expect("send_submit_task should succeed");

    // Wait for processing
    tokio::time::sleep(Duration::from_secs(2)).await;

    // All three receivers should have events
    let count1 = collect_available(&mut rx1);
    let count2 = collect_available(&mut rx2);
    let count3 = collect_available(&mut rx3);

    assert!(count1 > 0, "Receiver 1 should have received events");
    assert!(count2 > 0, "Receiver 2 should have received events");
    assert!(count3 > 0, "Receiver 3 should have received events");

    // All receivers should get the same number of events
    assert_eq!(count1, count2, "Receivers 1 and 2 should have same count");
    assert_eq!(count2, count3, "Receivers 2 and 3 should have same count");

    bridge.graceful_shutdown().await;
}

/// Helper: drain all available events from a broadcast receiver.
fn collect_available(rx: &mut tokio::sync::broadcast::Receiver<TaskEvent>) -> usize {
    let mut count = 0;
    while rx.try_recv().is_ok() {
        count += 1;
    }
    count
}
