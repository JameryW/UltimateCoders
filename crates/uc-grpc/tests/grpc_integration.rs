//! Integration test: start a gRPC server with LocalEngine (fallback),
//! connect a GrpcEngineClient, and exercise all RPCs.

use uc_engine::LocalEngine;
use uc_grpc::client::GrpcEngineClient;
use uc_grpc::server::GrpcServer;
use uc_types::EngineApi;

use tonic::transport::Server;

/// Helper: start a gRPC server on a random port and return the address.
async fn start_server() -> String {
    let engine = LocalEngine::new_fallback();
    let grpc_server = GrpcServer::new(engine);
    let (engine_service, task_service, _dashboard_service, _worker_service) =
        grpc_server.into_services();

    // Use port 0 to let OS pick a free port
    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().unwrap();
    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    let actual_addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        Server::builder()
            .add_service(engine_service)
            .add_service(task_service)
            .add_service(_dashboard_service)
            .add_service(_worker_service)
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
            .await
            .unwrap();
    });

    // Give the server a moment to start
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;

    format!("http://{}", actual_addr)
}

#[tokio::test]
async fn grpc_health_check() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let health = client.health().await.unwrap();
    assert_eq!(health.status, "degraded"); // Using fallback storage
    assert!(!health.version.is_empty());
    assert!(!health.components.is_empty());
}

#[tokio::test]
async fn grpc_memory_write_read_delete() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let key = uc_types::MemoryKey::Task {
        task_id: "grpc-test-task".to_string(),
        key: "decisions".to_string(),
    };

    // Write
    let entry = client
        .write_memory(uc_types::MemoryWriteRequest {
            key: key.clone(),
            content: uc_types::MemoryContent::Text("Use PostgreSQL for metadata".to_string()),
            metadata: uc_types::MemoryMetadata {
                source_agent: "grpc-test".to_string(),
                importance: 0.7,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            version: None,
        })
        .await
        .unwrap();

    assert_eq!(entry.key, key);

    // Read
    let read_result = client
        .read_memory(uc_types::MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        })
        .await
        .unwrap();

    assert!(read_result.is_some());
    let read_entry = read_result.unwrap();
    assert_eq!(read_entry.id, entry.id);

    // Delete
    client.delete_memory(&key).await.unwrap();

    // Verify deleted
    let after_delete = client
        .read_memory(uc_types::MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        })
        .await
        .unwrap();

    assert!(after_delete.is_none());
}

#[tokio::test]
async fn grpc_memory_global_key() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let key = uc_types::MemoryKey::Global {
        key: "global-config".to_string(),
    };

    client
        .write_memory(uc_types::MemoryWriteRequest {
            key: key.clone(),
            content: uc_types::MemoryContent::Text("v1".to_string()),
            metadata: uc_types::MemoryMetadata {
                source_agent: "grpc-test".to_string(),
                importance: 0.5,
                tags: vec![],
                embedding: None,
            },
            version: None,
        })
        .await
        .unwrap();

    let read = client
        .read_memory(uc_types::MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        })
        .await
        .unwrap();

    assert!(read.is_some());
}

#[tokio::test]
async fn grpc_memory_project_key() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let key = uc_types::MemoryKey::Project {
        project_id: "proj-1".to_string(),
        key: "architecture".to_string(),
    };

    client
        .write_memory(uc_types::MemoryWriteRequest {
            key: key.clone(),
            content: uc_types::MemoryContent::Structured(serde_json::json!({
                "type": "microservice",
                "components": ["engine", "grpc", "python"]
            })),
            metadata: uc_types::MemoryMetadata {
                source_agent: "grpc-test".to_string(),
                importance: 0.8,
                tags: vec!["architecture".to_string()],
                embedding: None,
            },
            version: None,
        })
        .await
        .unwrap();

    let read = client
        .read_memory(uc_types::MemoryReadRequest {
            key: key.clone(),
            include_semantic: false,
        })
        .await
        .unwrap();

    assert!(read.is_some());
    let entry = read.unwrap();
    match entry.content {
        uc_types::MemoryContent::Structured(v) => {
            assert_eq!(v["type"], "microservice");
        }
        _ => panic!("Expected structured content"),
    }
}

#[tokio::test]
async fn grpc_search_memory() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    // Write some entries to long-term memory (project-scoped)
    let key = uc_types::MemoryKey::Project {
        project_id: "proj-search".to_string(),
        key: "decision-db".to_string(),
    };

    client
        .write_memory(uc_types::MemoryWriteRequest {
            key: key.clone(),
            content: uc_types::MemoryContent::Text(
                "We chose PostgreSQL for structured metadata".to_string(),
            ),
            metadata: uc_types::MemoryMetadata {
                source_agent: "grpc-test".to_string(),
                importance: 0.9, // High importance -> stored in long-term
                tags: vec!["decision".to_string()],
                embedding: None,
            },
            version: None,
        })
        .await
        .unwrap();

    // Search memory
    let _result = client
        .search_memory(uc_types::MemorySearchRequest {
            query: "PostgreSQL metadata".to_string(),
            scope: uc_types::MemorySearchScope::Project {
                project_id: "proj-search".to_string(),
            },
            max_results: 10,
            min_score: 0.0, // Accept all results for testing
        })
        .await
        .unwrap();

    // With fallback storage, semantic search may not find results,
    // but the call itself should succeed.
    // In a real deployment with Qdrant, this would return results.
}

#[tokio::test]
async fn grpc_index_state_not_found() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let state = client.get_index_state("nonexistent-repo").await.unwrap();
    assert_eq!(state.repo_id, "nonexistent-repo");
    assert!(!state.indexed);
}

/// PR2: CreateCheckpoint + RecoverTask RPCs round-trip via the gRPC client.
/// submit_task records TaskCreated/SubtaskAssigned events to the EventStore;
/// recover_task replays them and reconstructs subtask state.
#[tokio::test]
async fn grpc_checkpoint_create_and_recover() {
    let endpoint = start_server().await;
    let client = GrpcEngineClient::connect(&endpoint).await.unwrap();

    let task = client
        .submit_task("checkpoint RPC test", "proj-ckpt")
        .await
        .unwrap();
    let task_id = task.id.0.clone();

    // record_event_with_subject appends to the EventStore via a spawned task
    // (fire-and-forget). Yield to let those appends land before snapshotting.
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;

    // Create a checkpoint — replays the event stream into a snapshot.
    let snapshot_id = client.create_checkpoint(&task_id).await.unwrap();
    assert!(!snapshot_id.is_empty());

    // Recover — should reconstruct the task by replaying the TaskCreated event.
    // submit_task_pending creates a Planning task with no subtasks, so the
    // snapshot has 0 subtasks but a non-trivial status from event replay.
    let snapshot = client.recover_task(&task_id).await.unwrap();
    assert_eq!(snapshot.task_id, task_id);
    assert_eq!(snapshot.subtasks.len(), 0);
    assert!(snapshot.last_event_offset > 0);
    assert!(!snapshot.status.is_empty());
}
