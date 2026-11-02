//! Integration test for the governance plane at the gateway boundary.
//!
//! Two kinds of coverage, deliberately separate:
//!
//! * **Operation tests** call the generated tonic traits directly on the service
//!   implementations. That is the same path a gRPC client takes minus the socket,
//!   so a failure here is a governance bug, not a transport flake.
//! * **One transport test** serves the four governance services on a real socket
//!   and calls them through generated clients, which is what proves
//!   `GrpcServer::with_governance` + `into_governance_services` actually wire a
//!   servable governance plane (a unit test cannot catch a registration mistake).
//!
//! The acceptance criteria of PR01 and PR03 are the first two tests: a replayed
//! submission returns the task it already created, and `ScaleWorkers` is refused
//! for a principal that does not hold `worker_scale`.

use std::sync::Arc;

use tonic::transport::Server;
use tonic::Request;

use uc_engine::governance::Governance;
use uc_engine::LocalEngine;
use uc_grpc::server::GrpcServer;
use uc_grpc::ultimate_coders::attention_service_server::AttentionService;
use uc_grpc::ultimate_coders::case_service_server::CaseService;
use uc_grpc::ultimate_coders::proactive_service_server::ProactiveService;
use uc_grpc::ultimate_coders::task_service_server::TaskService;
use uc_grpc::ultimate_coders::trigger_service_server::TriggerService;
use uc_grpc::ultimate_coders::worker_service_server::WorkerService;
use uc_grpc::ultimate_coders::*;
use uc_types::governance::{
    Grant, Permission, PolicySnapshot, PolicyVersion, Principal, WorkCase, CaseOrigin,
};

/// The operator surface identity used by the dashboard, per `policy_interceptor`.
const DASHBOARD_ID: &str = "dashboard";

/// Snapshot with exactly the permissions a test needs, so every test also proves
/// the door stays shut for everything it did not ask for.
async fn planned_governance(permissions: Vec<Permission>, principal: Principal) -> Arc<Governance> {
    let governance = Governance::in_memory();
    if !permissions.is_empty() {
        let snapshot = PolicySnapshot::empty(PolicyVersion::initial())
            .with_grant(Grant::new(principal, permissions))
            .expect("the test grants are structurally valid");
        governance
            .policy
            .install(snapshot)
            .await
            .expect("snapshot installs");
    }
    Arc::new(governance)
}

fn dashboard(permissions: Vec<Permission>) -> (Vec<Permission>, Principal) {
    (permissions, Principal::service(DASHBOARD_ID))
}

// ── PR01: submit-once ──────────────────────────────────────────────

/// Submit one task through the real `TaskService` implementation.
async fn submit_once(
    server: &GrpcServer<LocalEngine>,
    description: &str,
    submission_id: &str,
) -> SubmitTaskResponse {
    server
        .submit_task(Request::new(SubmitTaskRequest {
            description: description.to_string(),
            project_id: "proj-1".to_string(),
            submission_id: Some(submission_id.to_string()),
            ..Default::default()
        }))
        .await
        .expect("submit_task answers")
        .into_inner()
}

#[tokio::test]
async fn submit_task_replays_the_same_task_and_rejects_a_conflicting_reuse() {
    let server = GrpcServer::new(LocalEngine::new_fallback());

    let first = submit_once(&server, "Fix the build", "sub-key-1").await;
    assert!(first.success, "{:?}", first.error);
    assert!(!first.dedup_replayed, "first submission creates the task");
    assert_eq!(first.submission_id.as_deref(), Some("sub-key-1"));
    let task_id = first.task_id.clone();
    assert!(
        task_id.starts_with("sub-"),
        "the task id must be derived from the submission id, got {task_id}"
    );

    // Same key, same request: the replay must name the same task and create none.
    let replay = submit_once(&server, "Fix the build", "sub-key-1").await;
    assert!(replay.success, "{:?}", replay.error);
    assert!(replay.dedup_replayed, "a replay must be reported as such");
    assert_eq!(replay.task_id, task_id, "replay must not create a second task");
    let tasks = server.task_store().lock().await.list_tasks();
    assert_eq!(tasks.len(), 1, "exactly one task exists after a replay");

    // Same key, different request: a conflict, not a silent replay.
    let conflict = submit_once(&server, "Fix something else entirely", "sub-key-1").await;
    assert!(!conflict.success, "a conflicting reuse must be refused");
    let error = conflict.error.unwrap_or_default();
    assert!(
        error.contains("sub-key-1") && error.contains("different request"),
        "the refusal must name the reused key, got: {error}"
    );
    let tasks = server.task_store().lock().await.list_tasks();
    assert_eq!(tasks.len(), 1, "a conflict must not create a task");
}

#[tokio::test]
async fn two_caller_supplied_submission_ids_are_two_distinct_tasks() {
    let server = GrpcServer::new(LocalEngine::new_fallback());
    let mut task_ids = Vec::new();
    for key in ["key-a", "key-b"] {
        let response = server
            .submit_task(Request::new(SubmitTaskRequest {
                description: "Same description".to_string(),
                project_id: "proj-1".to_string(),
                submission_id: Some(key.to_string()),
                ..Default::default()
            }))
            .await
            .expect("submit_task answers")
            .into_inner();
        assert!(response.success, "{:?}", response.error);
        task_ids.push(response.task_id);
    }
    assert_ne!(
        task_ids[0], task_ids[1],
        "distinct idempotency keys are distinct submissions"
    );
}

// ── PR03: ScaleWorkers authorization ───────────────────────────────

#[tokio::test]
async fn scale_workers_is_denied_for_a_service_principal_without_worker_scale() {
    let server = GrpcServer::new(LocalEngine::new_fallback());

    let error = server
        .scale_workers(Request::new(ScaleWorkersRequest {
            action: "deregister".to_string(),
            target_count: 0,
            worker_id: "worker-1".to_string(),
        }))
        .await
        .expect_err("a principal without worker_scale must be refused");
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    assert!(
        error.message().starts_with("missing_permission"),
        "the status must carry the deny code, got: {}",
        error.message()
    );

    // An action that would otherwise shell out to docker is also refused before
    // any side effect: the denial happens before the dry-run/scale path.
    let error = server
        .scale_workers(Request::new(ScaleWorkersRequest {
            action: "scale".to_string(),
            target_count: 3,
            worker_id: String::new(),
        }))
        .await
        .expect_err("scale must be refused as well");
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
}

#[tokio::test]
async fn scale_workers_reaches_the_action_when_worker_scale_is_granted() {
    let (permissions, principal) = dashboard(vec![Permission::WorkerScale]);
    let governance = planned_governance(permissions, principal).await;
    let server = GrpcServer::new(LocalEngine::new_fallback()).with_governance(governance);

    // `deregister` with no worker id is refused by the action's own validation,
    // which only runs once the policy gate has passed — proof the gate opened.
    let response = server
        .scale_workers(Request::new(ScaleWorkersRequest {
            action: "deregister".to_string(),
            target_count: 0,
            worker_id: String::new(),
        }))
        .await
        .expect("an authorized caller reaches the action")
        .into_inner();
    assert!(!response.success);
    assert!(
        response
            .error
            .unwrap_or_default()
            .contains("worker_id is required"),
        "the action's own validation must answer, not the policy gate"
    );
}

// ── Cases: create → run → finish → completed ───────────────────────

#[tokio::test]
async fn a_case_runs_to_completion_and_never_skips_verification() {
    let (permissions, principal) = dashboard(vec![
        Permission::CaseCreate,
        Permission::CaseControl,
        Permission::MandateIssue,
    ]);
    let governance = planned_governance(permissions, principal).await;

    let service = uc_grpc::governance::CaseServiceImpl::new(governance.clone());

    let created = service
        .create_case(Request::new(CreateCaseRequest {
            title: "Fix the flaky test".to_string(),
            description: "The suite is red on main".to_string(),
            project_id: "proj-1".to_string(),
            source: "manual".to_string(),
            external_ref: Some("acme/widgets#7".to_string()),
            thread_key: None,
            trigger_id: None,
            priority: Some(4),
            completion_conditions_json: Some(r#"{"tests":"green"}"#.to_string()),
            contract_digest: None,
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
            issue_mandate: true,
            mandate_scopes_json: Some(
                r#"[{"kind":"tool","name":"Read"},{"kind":"action","class":"read"}]"#.to_string(),
            ),
            mandate_expires_at: None,
            labels: vec!["ci".to_string()],
        }))
        .await
        .expect("case created")
        .into_inner();
    assert!(created.success, "{:?}", created.error);
    let case = created.case.expect("case present");
    let mandate = created.mandate.expect("mandate issued with the case");
    assert_eq!(case.mandate_id.as_deref(), Some(mandate.id.as_str()));
    assert_eq!(case.origin.expect("origin present").source, "manual");
    assert_eq!(case.priority, 4);

    let service = uc_grpc::governance::CaseServiceImpl::new(governance.clone());

    let active = service
        .transition_case(Request::new(TransitionCaseRequest {
            case_id: case.id.clone(),
            status: "active".to_string(),
            reason: Some("starting".to_string()),
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
        }))
        .await
        .expect("transition allowed")
        .into_inner();
    assert!(active.success);
    assert_eq!(active.case.expect("case present").status, "active");

    let run = service
        .open_case_run(Request::new(OpenCaseRunRequest {
            case_id: case.id.clone(),
            task_id: "task-run-1".to_string(),
            submission_id: Some("sub-1".to_string()),
        }))
        .await
        .expect("run opened")
        .into_inner();
    assert!(run.success, "{:?}", run.error);
    let run = run.run.expect("run present");
    assert_eq!(run.attempt, 1);
    assert_eq!(run.submission_id.as_deref(), Some("sub-1"));

    let verifying = service
        .finish_case_run(Request::new(FinishCaseRunRequest {
            case_id: case.id.clone(),
            run_id: run.run_id.clone(),
            outcome: "succeeded".to_string(),
            detail: None,
        }))
        .await
        .expect("run finished")
        .into_inner();
    assert_eq!(
        verifying.case.expect("case present").status,
        "verifying",
        "success must pass through independent verification"
    );

    let completed = service
        .transition_case(Request::new(TransitionCaseRequest {
            case_id: case.id.clone(),
            status: "completed".to_string(),
            reason: None,
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
        }))
        .await
        .expect("verification completed the case")
        .into_inner();
    assert_eq!(completed.case.expect("case present").status, "completed");
}

// ── Triggers: dedupe ──────────────────────────────────────────────

#[tokio::test]
async fn a_redelivered_trigger_is_ingested_once() {
    let (permissions, principal) = dashboard(vec![Permission::TriggerIngest]);
    let governance = planned_governance(permissions, principal).await;
    let service = uc_grpc::governance::TriggerServiceImpl::new(governance);

    let request = || {
        Request::new(IngestTriggerRequest {
            source: "github".to_string(),
            kind: "pull_request_comment".to_string(),
            external_id: "comment-7".to_string(),
            thread_key: Some("acme/widgets#7".to_string()),
            repo: Some("acme/widgets".to_string()),
            actor: Some("alice".to_string()),
            payload_json: r#"{"comment":"please fix"}"#.to_string(),
            delivery_id: Some("delivery-1".to_string()),
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
            open_case_if_unmatched: false,
            open_case_project_id: None,
        })
    };

    let first = service
        .ingest_trigger(request())
        .await
        .expect("ingest answers")
        .into_inner();
    assert!(first.success);
    assert!(first.accepted);
    assert!(!first.duplicate);
    assert_eq!(
        first.unmatched_reason.as_deref(),
        Some("thread_has_no_subscription")
    );

    let second = service
        .ingest_trigger(request())
        .await
        .expect("ingest answers")
        .into_inner();
    assert!(second.duplicate, "a redelivered event must be a duplicate");
    assert!(!second.accepted);
    assert_eq!(second.dedup_key, first.dedup_key);
}

// ── Attention: raise → resolve → capacity ─────────────────────────

#[tokio::test]
async fn attention_items_are_raised_resolved_and_counted() {
    let (permissions, principal) = dashboard(vec![
        Permission::CaseCreate,
        Permission::CaseControl,
        Permission::AttentionResolve,
    ]);
    let governance = planned_governance(permissions, principal).await;
    let now = chrono::Utc::now();
    let case = governance
        .cases
        .create_unique(WorkCase::new("case", "desc", "proj-1", CaseOrigin::manual(), now))
        .await
        .expect("case created")
        .0;
    let service = uc_grpc::governance::AttentionServiceImpl::new(governance);

    let raised = service
        .raise_attention(Request::new(RaiseAttentionRequest {
            case_id: case.id.0.clone(),
            kind: "needs_human".to_string(),
            summary: "Pick a release branch".to_string(),
            detail: Some("main is frozen".to_string()),
            human_action: Some("choose release/2.0 or hotfix".to_string()),
            task_id: Some("task-1".to_string()),
            priority: Some(8),
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
        }))
        .await
        .expect("raise answers")
        .into_inner();
    assert!(raised.success, "{:?}", raised.error);
    let item = raised.item.expect("item present");
    assert_eq!(item.kind, "needs_human");
    assert_eq!(item.priority, 8);

    let listed = service
        .list_attention(Request::new(ListAttentionRequest {
            include_resolved: false,
            case_id: Some(case.id.0.clone()),
            kind: None,
            limit: 10,
        }))
        .await
        .expect("list answers")
        .into_inner();
    assert_eq!(listed.items.len(), 1);
    let capacity = listed.capacity.expect("capacity always accompanies the list");
    assert_eq!(capacity.needs_human_items, 1);

    let resolved = service
        .resolve_attention(Request::new(ResolveAttentionRequest {
            attention_id: item.id.clone(),
            resolution_kind: "human_decision".to_string(),
            by: Some("alice".to_string()),
            detail: Some("release/2.0".to_string()),
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
        }))
        .await
        .expect("resolve answers")
        .into_inner();
    assert!(resolved.success, "{:?}", resolved.error);
    let resolved_item = resolved.item.expect("item present");
    assert_eq!(
        resolved_item.resolution_summary.as_deref(),
        Some("human:alice:release/2.0")
    );

    let capacity = service
        .get_capacity(Request::new(GetCapacityRequest {}))
        .await
        .expect("capacity answers")
        .into_inner()
        .capacity
        .expect("capacity present");
    assert_eq!(capacity.needs_human_items, 0);
    assert_eq!(
        capacity.slots_saved_by_quiet_waiting,
        capacity
            .waiting_event_cases
            .saturating_add(capacity.verifying_cases)
    );
}

// ── Proactive: off by default ─────────────────────────────────────

#[tokio::test]
async fn proactive_mode_off_refuses_to_record_a_proposal() {
    let (permissions, principal) = dashboard(vec![Permission::ProactivePropose]);
    let governance = planned_governance(permissions, principal).await;
    let service = uc_grpc::governance::ProactiveServiceImpl::new(governance);

    let state = service
        .get_proactive_state(Request::new(GetProactiveStateRequest {}))
        .await
        .expect("state answers")
        .into_inner()
        .state
        .expect("state present");
    assert_eq!(state.mode, "off", "the default mode must be off");

    let submitted = service
        .submit_proposal(Request::new(SubmitProposalRequest {
            case_id: None,
            kind: "fix_failing_ci".to_string(),
            title: "Fix CI run run-42".to_string(),
            rationale: "CI run run-42 concluded failure".to_string(),
            confidence: 0.8,
            evidence: vec!["ci:run-42".to_string()],
            proposed_request: Some("Fix the failing test".to_string()),
            project_id: Some("proj-1".to_string()),
            estimated_tokens: Some(10_000),
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
        }))
        .await
        .expect("submit answers")
        .into_inner();
    assert!(submitted.success);
    assert!(!submitted.accepted, "off mode must not admit proposals");
    assert_eq!(
        submitted.deny_code.as_deref(),
        Some("proactive_mode_disallows")
    );

    // The refusal is auditable, which is the point of recording it.
    let audit = service
        .list_audit(Request::new(ListAuditRequest {
            limit: 10,
            proposal_id: None,
            case_id: None,
        }))
        .await
        .expect("audit answers")
        .into_inner();
    assert!(audit.entries.iter().any(|entry| !entry.allowed));
}

// ── Transport: the four services actually serve ───────────────────

#[tokio::test]
async fn the_governance_services_answer_over_a_real_grpc_connection() {
    let (permissions, principal) = dashboard(vec![
        Permission::CaseCreate,
        Permission::CaseRead,
    ]);
    let governance = planned_governance(permissions, principal).await;

    let server = GrpcServer::new(LocalEngine::new_fallback()).with_governance(governance);
    let (case_service, trigger_service, attention_service, proactive_service) =
        server.into_governance_services();

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind a test listener");
    let address = listener.local_addr().expect("listener address");
    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);
    tokio::spawn(async move {
        Server::builder()
            .add_service(case_service)
            .add_service(trigger_service)
            .add_service(attention_service)
            .add_service(proactive_service)
            .serve_with_incoming(incoming)
            .await
            .expect("test server serves");
    });

    let endpoint = format!("http://{address}");
    let mut client = case_service_client::CaseServiceClient::connect(endpoint.clone())
        .await
        .expect("connect to the case service");

    let created = client
        .create_case(Request::new(CreateCaseRequest {
            title: "Over the wire".to_string(),
            description: "Real transport".to_string(),
            project_id: "proj-1".to_string(),
            source: "manual".to_string(),
            external_ref: Some("acme/widgets#11".to_string()),
            thread_key: None,
            trigger_id: None,
            priority: None,
            completion_conditions_json: None,
            contract_digest: None,
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
            issue_mandate: false,
            mandate_scopes_json: None,
            mandate_expires_at: None,
            labels: Vec::new(),
        }))
        .await
        .expect("create over gRPC")
        .into_inner();
    assert!(created.success, "{:?}", created.error);
    let case = created.case.expect("case present");
    assert_eq!(case.project_id, "proj-1");
    // Proto timestamps are Unix seconds; the bridge uses RFC 3339 for the same
    // value (see `governance::bridge`).
    assert!(case.created_at > 0);

    let fetched = client
        .get_case(Request::new(GetCaseRequest {
            case_id: case.id.clone(),
        }))
        .await
        .expect("get over gRPC")
        .into_inner();
    assert!(fetched.available);
    assert_eq!(fetched.case.expect("case present").id, case.id);

    // The proactive service is reachable on the same server.
    let mut proactive = proactive_service_client::ProactiveServiceClient::connect(endpoint)
        .await
        .expect("connect to the proactive service");
    let state = proactive
        .get_proactive_state(Request::new(GetProactiveStateRequest {}))
        .await
        .expect("state over gRPC")
        .into_inner()
        .state
        .expect("state present");
    assert_eq!(state.mode, "off");
}

// ── Waiting-event lifecycle: an arriving event wakes its case ──────

/// A case parked in `waiting_event` holds no worker slot. The only thing that
/// keeps that honest is an arriving subscribed event ending the wait — and it has
/// to happen through the *service* path, because that is the path the dashboard
/// and the webhook use. This test drives ingest over the gRPC impl and then reads
/// the case and the attention item back through their own services.
#[tokio::test]
async fn an_arriving_event_wakes_a_waiting_case() {
    use uc_types::governance::{
        AttentionItem, AttentionKind, CaseStatus, SourceKind, Subscription, SubscriptionFilter,
    };

    let (permissions, principal) = dashboard(vec![
        Permission::CaseCreate,
        Permission::CaseControl,
        Permission::TriggerIngest,
        Permission::AttentionResolve,
    ]);
    let governance = planned_governance(permissions, principal).await;
    let now = chrono::Utc::now();

    // Seed the waiting state directly: creating a case and parking it is not the
    // behaviour under test, and routing it through RPCs would need permissions
    // this scenario deliberately does not grant.
    let mut case = WorkCase::new(
        "flaky build",
        "the build keeps failing",
        "proj",
        CaseOrigin::from_trigger("github", Some("acme/widgets#7".to_string()), None),
        now,
    );
    case.status = CaseStatus::WaitingEvent;
    let (case, _) = governance.cases.create_unique(case).await.expect("case");
    let case_id = case.id.0.clone();
    governance
        .triggers
        .upsert_subscription(Subscription::new(
            case_id.clone(),
            SourceKind::GitHub,
            SubscriptionFilter::thread("acme/widgets#7"),
            now,
        ))
        .await
        .expect("subscription");
    governance
        .attention
        .raise(
            AttentionItem::new(
                "att-waiting",
                case_id.clone(),
                AttentionKind::WaitingEvent,
                "waiting on the CI run",
                now,
            ),
            now,
        )
        .await
        .expect("attention item");

    let triggers = uc_grpc::governance::TriggerServiceImpl::new(governance.clone());
    let cases = uc_grpc::governance::CaseServiceImpl::new(governance.clone());
    let attention = uc_grpc::governance::AttentionServiceImpl::new(governance.clone());

    let ingested = triggers
        .ingest_trigger(Request::new(IngestTriggerRequest {
            source: "github".to_string(),
            kind: "check_run".to_string(),
            external_id: "check_run:42:completed:success".to_string(),
            thread_key: Some("acme/widgets#7".to_string()),
            repo: Some("acme/widgets".to_string()),
            actor: Some("github-actions".to_string()),
            payload_json: r#"{"conclusion":"success"}"#.to_string(),
            delivery_id: None,
            principal_kind: "service".to_string(),
            principal_id: DASHBOARD_ID.to_string(),
            open_case_if_unmatched: false,
            open_case_project_id: None,
        }))
        .await
        .expect("ingest answers")
        .into_inner();
    assert!(ingested.success);
    assert_eq!(ingested.matched_cases, vec![case_id.clone()]);

    // The case left `waiting_event` …
    let fetched = cases
        .get_case(Request::new(GetCaseRequest {
            case_id: case_id.clone(),
        }))
        .await
        .expect("get case answers")
        .into_inner();
    assert_eq!(
        fetched.case.expect("case present").status,
        "active",
        "an arriving subscribed event must wake the case"
    );

    // … and the waiting item is resolved, not left in the worklist.
    let listed = attention
        .list_attention(Request::new(ListAttentionRequest {
            include_resolved: true,
            case_id: Some(case_id),
            kind: None,
            limit: 10,
        }))
        .await
        .expect("list attention answers")
        .into_inner();
    let item = listed
        .items
        .iter()
        .find(|item| item.id == "att-waiting")
        .expect("the waiting item is still readable");
    assert!(
        item.resolved_at.is_some(),
        "the waiting item must be resolved by the arriving event"
    );
}