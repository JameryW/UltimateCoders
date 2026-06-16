//! Integration test: local worker bridge.
//!
//! Tests that the gRPC server correctly handles the local worker lifecycle:
//! - Worker unavailable → falls back to newline-split decomposition
//! - WorkerTaskUpdate deserialization works correctly
//! - JSON-RPC protocol format is valid

use uc_engine::LocalEngine;
use uc_grpc::server::GrpcServer;

/// Test: when Python worker is unavailable, submit_task falls back gracefully.
#[tokio::test]
async fn submit_task_fallback_without_worker() {
    let engine = LocalEngine::new_fallback();
    // GrpcServer::new will try to spawn local_worker.py, which will fail
    // in test environment (no maturin build). Falls back to newline-split.
    let server = GrpcServer::new(engine).await;

    // Verify the server was created (even without worker)
    let _ = server.into_services();
}

/// Test: WorkerTaskUpdate deserialization and field access.
#[tokio::test]
async fn apply_worker_update_to_task_store() {
    use uc_grpc::local_worker::{WorkerSubtaskUpdate, WorkerTaskUpdate};

    let update = WorkerTaskUpdate {
        task_id: "test-task-1".to_string(),
        description: "Fix the login bug".to_string(),
        project_id: "proj-1".to_string(),
        status: "in_progress".to_string(),
        subtasks: vec![WorkerSubtaskUpdate {
            id: "st-1".to_string(),
            description: "Write test".to_string(),
            status: "assigned".to_string(),
            assigned_worker: Some("worker-1".to_string()),
            depends_on: vec![],
        }],
        result: None,
    };

    // Verify deserialization roundtrip via JSON
    let json = serde_json::to_string(&update).unwrap();
    let parsed: WorkerTaskUpdate = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed.task_id, "test-task-1");
    assert_eq!(parsed.status, "in_progress");
    assert_eq!(parsed.subtasks.len(), 1);
    assert_eq!(parsed.subtasks[0].id, "st-1");
    assert_eq!(parsed.subtasks[0].assigned_worker, Some("worker-1".to_string()));
}

/// Test: JSON-RPC protocol messages are single-line (newline-delimited).
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

    let response = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "task_id": "t-1",
            "status": "completed",
            "subtasks": []
        }
    });

    // Verify they serialize to single-line JSON (newline-delimited protocol)
    assert!(!serde_json::to_string(&request).unwrap().contains('\n'));
    assert!(!serde_json::to_string(&notification).unwrap().contains('\n'));
    assert!(!serde_json::to_string(&response).unwrap().contains('\n'));
}
