//! Opt-in checks against the deployed Gateway; creates only a blocked fixture task.

use uc_grpc::ultimate_coders::dashboard_service_client::DashboardServiceClient;
use uc_grpc::ultimate_coders::task_service_client::TaskServiceClient;
use uc_grpc::ultimate_coders::*;

#[tokio::test]
#[ignore = "requires an explicitly selected deployed Gateway via UC_GATEWAY_TEST_ADDR"]
async fn deployed_gateway_task_controls_and_checkpoint_round_trip() {
    let endpoint = std::env::var("UC_GATEWAY_TEST_ADDR").expect("set UC_GATEWAY_TEST_ADDR");
    let mut client = TaskServiceClient::connect(endpoint).await.unwrap();
    let id = format!("deployment-control-{}", uuid::Uuid::new_v4());
    let node = format!("{id}-node");
    let created = client
        .update_task(UpdateTaskRequest {
            task_id: id.clone(),
            description: "Blocked deployment control fixture".into(),
            project_id: "verification".into(),
            status: "InProgress".into(),
            subtasks: vec![SubtaskProto {
                id: node,
                description: "Do not execute".into(),
                status: "Pending".into(),
                required_capabilities: vec![format!("unavailable-{id}")],
                ..Default::default()
            }],
        })
        .await
        .unwrap()
        .into_inner();
    assert!(created.success, "{created:?}");
    let paused = client
        .pause_task(PauseTaskRequest {
            task_id: id.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(paused.success, "{paused:?}");
    assert_eq!(paused.status, "Paused");
    let snapshot = client
        .create_checkpoint(CreateCheckpointRequest {
            task_id: id.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(snapshot.success, "{snapshot:?}");
    assert!(!snapshot.snapshot_id.is_empty());
    let recovered = client
        .recover_task(RecoverTaskRequest {
            task_id: id.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(recovered.success, "{recovered:?}");
    assert_eq!(recovered.snapshot.unwrap().status, "paused");
    let resumed = client
        .resume_task(ResumeTaskRequest {
            task_id: id.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(resumed.success, "{resumed:?}");
    assert_eq!(resumed.status, "InProgress");
    let cancelled = client
        .cancel_task(CancelTaskRequest {
            task_id: id.clone(),
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(cancelled.success, "{cancelled:?}");
    assert_eq!(cancelled.status, "Failed");
    let task = client
        .get_task(GetTaskRequest {
            task_id: id.clone(),
        })
        .await
        .unwrap()
        .into_inner()
        .task
        .unwrap();
    assert_eq!(task.status, "Failed");
    assert!(task.subtasks.iter().all(|node| node.status == "Failed"));
    assert!(
        !client
            .resume_task(ResumeTaskRequest { task_id: id })
            .await
            .unwrap()
            .into_inner()
            .success,
        "a cancelled task cannot resume"
    );
}

#[tokio::test]
#[ignore = "requires an explicitly selected deployed Gateway via UC_GATEWAY_TEST_ADDR"]
async fn deployed_gateway_scheduler_job_controls() {
    let endpoint = std::env::var("UC_GATEWAY_TEST_ADDR").expect("set UC_GATEWAY_TEST_ADDR");
    let mut client = DashboardServiceClient::connect(endpoint).await.unwrap();
    assert!(
        client
            .get_scheduler_status(GetSchedulerStatusRequest {})
            .await
            .unwrap()
            .into_inner()
            .available
    );
    let invalid = client
        .add_cron_job(AddCronJobRequest {
            description: "Invalid deployment fixture".into(),
            cron_expression: "not-a-cron".into(),
            project_id: "verification".into(),
            timezone: "UTC".into(),
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(!invalid.success);
    let created = client
        .add_cron_job(AddCronJobRequest {
            description: "Deployment scheduler fixture: do not execute".into(),
            cron_expression: "0 0 0 1 1 *".into(),
            project_id: "verification".into(),
            timezone: "UTC".into(),
            enabled: false,
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(created.success, "{created:?}");
    let id = created.job_id;
    for enabled in [true, false] {
        let toggled = client
            .set_scheduler_job_enabled(SetSchedulerJobEnabledRequest {
                job_id: id.clone(),
                enabled,
            })
            .await
            .unwrap()
            .into_inner();
        assert!(toggled.success, "{toggled:?}");
        let status = client
            .get_scheduler_status(GetSchedulerStatusRequest {})
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            status.jobs.iter().find(|job| job.id == id).unwrap().enabled,
            enabled
        );
    }
    let removed = client
        .remove_job(RemoveJobRequest { job_id: id.clone() })
        .await
        .unwrap()
        .into_inner();
    assert!(removed.success, "{removed:?}");
    assert!(!client
        .get_scheduler_status(GetSchedulerStatusRequest {})
        .await
        .unwrap()
        .into_inner()
        .jobs
        .iter()
        .any(|job| job.id == id));
}
