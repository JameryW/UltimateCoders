#![cfg(feature = "messaging")]

use futures::StreamExt;
use serde_json::json;
use std::time::{Duration, Instant};
use tonic::Request;
use uc_grpc::server::GrpcServer;
use uc_grpc::ultimate_coders::task_service_server::TaskService;
use uc_grpc::ultimate_coders::worker_service_server::WorkerService;
use uc_grpc::ultimate_coders::{
    ListTasksRequest, RecoverTaskRequest, RegisterWorkerRequest, RetrySubtaskRequest,
    WatchTaskRequest,
};

async fn make_task_dispatch(
    dispatches: &mut async_nats::Subscriber,
    task_id: &str,
) -> serde_json::Value {
    loop {
        let message = dispatches
            .next()
            .await
            .expect("dispatch subscription closed");
        let payload: serde_json::Value = serde_json::from_slice(&message.payload).unwrap();
        if payload["graph_id"] == task_id {
            return payload;
        }
    }
}

#[tokio::test]
#[ignore = "requires an isolated NATS broker via UC_NATS_TEST_URL"]
async fn completed_snapshot_is_broadcast_once_and_survives_late_worker_update() {
    let url = std::env::var("UC_NATS_TEST_URL").expect("set UC_NATS_TEST_URL");
    let client = async_nats::connect(&url).await.unwrap();
    let server = GrpcServer::with_nats(uc_engine::LocalEngine::new_fallback(), &url).await;
    let id = format!("snapshot-lifecycle-{}", uuid::Uuid::new_v4());
    let node = format!("{id}-node");
    let mut events = server
        .watch_task(Request::new(WatchTaskRequest {
            task_id: id.clone(),
        }))
        .await
        .unwrap()
        .into_inner();
    let mut snapshot = json!({
        "task_id": id, "description": "Snapshot lifecycle fixture",
        "project_id": "verification", "status": "InProgress", "partial": false,
        "subtasks": [{"subtask_id": node, "status": "Pending", "depends_on": []}]
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        client
            .publish("uc.task.update", snapshot.to_string().into())
            .await
            .unwrap();
        client.flush().await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        let tasks = server
            .list_tasks(Request::new(ListTasksRequest::default()))
            .await
            .unwrap()
            .into_inner()
            .tasks;
        if tasks.iter().any(|t| t.id == id) {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "subscriber never accepted the initial snapshot"
        );
    }
    snapshot["subtasks"][0]["status"] = json!("InProgress");
    for suffix in ["started", "same-state"] {
        snapshot["message_id"] = json!(format!("{id}-{suffix}"));
        client
            .publish("uc.task.update", snapshot.to_string().into())
            .await
            .unwrap();
    }
    snapshot["status"] = json!("Completed");
    snapshot["subtasks"][0]["status"] = json!("Completed");
    for message_id in ["completed-once", "completed-again"] {
        snapshot["message_id"] = json!(format!("{id}-{message_id}"));
        client
            .publish("uc.task.update", snapshot.to_string().into())
            .await
            .unwrap();
    }
    snapshot["partial"] = json!(true);
    snapshot["status"] = json!("InProgress");
    snapshot["message_id"] = json!(format!("{id}-late-worker"));
    snapshot["subtasks"][0]["result"] = json!("late-worker-result");
    client
        .publish("uc.task.update", snapshot.to_string().into())
        .await
        .unwrap();
    client.flush().await.unwrap();
    loop {
        let tasks = server
            .list_tasks(Request::new(ListTasksRequest::default()))
            .await
            .unwrap()
            .into_inner()
            .tasks;
        let task = tasks.iter().find(|t| t.id == id).unwrap();
        if task
            .subtasks
            .iter()
            .any(|s| s.result.as_deref() == Some("late-worker-result"))
        {
            assert_eq!(task.status, "Completed");
            break;
        }
        assert!(
            Instant::now() < deadline,
            "late worker update was not processed"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let mut completed = 0;
    let mut started = 0;
    while let Ok(Some(event)) =
        tokio::time::timeout(Duration::from_millis(300), events.next()).await
    {
        match event.unwrap().r#type.as_str() {
            "task_completed" => completed += 1,
            "subtask_started" => started += 1,
            _ => {}
        }
    }
    assert_eq!(
        completed, 1,
        "terminal lifecycle must be observable once through WatchTask"
    );
    assert_eq!(started, 1, "unchanged snapshots must not repeat Started");
}

#[tokio::test]
#[ignore = "requires an isolated NATS broker via UC_NATS_TEST_URL"]
async fn snapshot_configuration_reaches_capability_matched_worker_dispatch() {
    let url = std::env::var("UC_NATS_TEST_URL").expect("set UC_NATS_TEST_URL");
    let client = async_nats::connect(&url).await.unwrap();
    let server = GrpcServer::with_nats(uc_engine::LocalEngine::new_fallback(), &url).await;
    let id = format!("snapshot-config-{}", uuid::Uuid::new_v4());
    let mut dispatches = client.subscribe("uc.subtask.execute").await.unwrap();
    client.flush().await.unwrap();
    let register = |caps: Vec<String>| RegisterWorkerRequest {
        worker_id: format!("{id}-worker"),
        capabilities: caps,
        max_capacity: 1,
        metadata: String::new(),
        contract_version: uc_types::CONTRACT_VERSION.into(),
        projects: vec![id.clone()],
    };
    assert!(
        server
            .register_worker(Request::new(register(vec!["python".into()])))
            .await
            .unwrap()
            .into_inner()
            .success
    );
    let config = json!({"agent": "local-harness", "max_turns": 12,
        "inference_task": {"workload_id": "ollama-chat"}});
    let snapshot = json!({
        "task_id": id, "description": "Wire configuration fixture",
        "project_id": id, "status": "InProgress", "partial": false,
        "subtasks": [{"subtask_id": format!("{id}-node"), "status": "Pending",
            "description": "verify", "agent_config_json": config.to_string(),
            "required_capabilities": ["inference_infra"],
            "file_constraints": ["calculator.py"], "expected_output": "passing tests",
            "workflow_steps": [{"agent": "local-harness", "prompt": "verify",
                "agent_config_json": "{\"max_turns\":4}"}]}]
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        client
            .publish("uc.task.update", snapshot.to_string().into())
            .await
            .unwrap();
        client.flush().await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        if server
            .list_tasks(Request::new(ListTasksRequest::default()))
            .await
            .unwrap()
            .into_inner()
            .tasks
            .iter()
            .any(|task| task.id == id)
        {
            break;
        }
        assert!(Instant::now() < deadline, "snapshot not accepted");
    }
    assert!(
        tokio::time::timeout(
            Duration::from_millis(200),
            make_task_dispatch(&mut dispatches, &id)
        )
        .await
        .is_err(),
        "worker without inference capability must not receive this task"
    );
    let mut worker_report = snapshot.clone();
    worker_report["partial"] = json!(true);
    worker_report["subtasks"][0]["required_capabilities"] = json!([]);
    worker_report["subtasks"][0]["agent_config_json"] = json!("{\"max_turns\":999}");
    client
        .publish("uc.task.update", worker_report.to_string().into())
        .await
        .unwrap();
    client.flush().await.unwrap();
    assert!(
        tokio::time::timeout(
            Duration::from_millis(200),
            make_task_dispatch(&mut dispatches, &id)
        )
        .await
        .is_err(),
        "partial worker report must not remove the capability gate"
    );
    assert!(
        server
            .register_worker(Request::new(register(vec!["inference_infra".into()])))
            .await
            .unwrap()
            .into_inner()
            .success
    );
    client
        .publish("uc.task.update", snapshot.to_string().into())
        .await
        .unwrap();
    client.flush().await.unwrap();
    let payload = tokio::time::timeout(
        Duration::from_secs(5),
        make_task_dispatch(&mut dispatches, &id),
    )
    .await
    .unwrap();
    assert_eq!(payload["graph_id"], id);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(payload["agent_config_json"].as_str().unwrap())
            .unwrap(),
        config
    );
    assert_eq!(payload["required_capabilities"], json!(["inference_infra"]));
    assert_eq!(payload["file_constraints"], json!(["calculator.py"]));
    assert_eq!(payload["expected_output"], "passing tests");
    assert_eq!(
        payload["steps"][0]["agent_config_json"],
        "{\"max_turns\":4}"
    );
}

#[tokio::test]
#[ignore = "requires an isolated NATS broker via UC_NATS_TEST_URL"]
async fn explicit_retry_snapshot_reopens_durable_parent_state() {
    let url = std::env::var("UC_NATS_TEST_URL").expect("set UC_NATS_TEST_URL");
    let client = async_nats::connect(&url).await.unwrap();
    let server = GrpcServer::with_nats(uc_engine::LocalEngine::new_fallback(), &url).await;
    let id = format!("snapshot-retry-{}", uuid::Uuid::new_v4());
    let node = format!("{id}-node");
    for (parent, child) in [("Failed", "Failed"), ("InProgress", "Assigned")] {
        if parent == "InProgress" {
            // Only an explicit Gateway command can reopen terminal control state.
            // The following coordinator snapshot advances this reset node to Assigned.
            let retry = server
                .retry_subtask(Request::new(RetrySubtaskRequest {
                    task_id: id.clone(),
                    subtask_id: node.clone(),
                    expected_attempt: 0,
                }))
                .await
                .unwrap()
                .into_inner();
            assert!(retry.success, "{retry:?}");
        }
        let snapshot = json!({
            "task_id": id, "description": "Retry lifecycle fixture",
            "project_id": "verification", "status": parent, "partial": false,
            "message_id": format!("{id}-{parent}"),
            "subtasks": [{"subtask_id": node, "status": child,
                "attempt_id": if parent == "Failed" { 0 } else { 1 }}]
        });
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            client
                .publish("uc.task.update", snapshot.to_string().into())
                .await
                .unwrap();
            client.flush().await.unwrap();
            tokio::time::sleep(Duration::from_millis(20)).await;
            if server
                .list_tasks(Request::new(ListTasksRequest::default()))
                .await
                .unwrap()
                .into_inner()
                .tasks
                .iter()
                .any(|task| {
                    task.id == id
                        && task.status == parent
                        && task
                            .subtasks
                            .iter()
                            .any(|st| st.id == node && st.status == child)
                })
            {
                break;
            }
            assert!(Instant::now() < deadline, "snapshot not applied");
        }
        let recovery = server
            .recover_task(Request::new(RecoverTaskRequest {
                task_id: id.clone(),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(recovery.success, "{recovery:?}");
        assert_eq!(
            recovery.snapshot.unwrap().status,
            if parent == "Failed" {
                "failed"
            } else {
                "in_progress"
            }
        );
    }
}
