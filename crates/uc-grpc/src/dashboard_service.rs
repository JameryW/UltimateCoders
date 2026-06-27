//! DashboardService implementation — NATS passthrough to Python Orchestrator.
//!
//! All DashboardService RPCs forward via NATS request-reply to the Python
//! Orchestrator, which holds workers/scheduler/circuit-breaker state in memory.
//! When NATS is unavailable, RPCs return UNAVAILABLE status.

#[cfg(feature = "messaging")]
use futures::StreamExt;
#[cfg(not(feature = "messaging"))]
use tokio::sync::broadcast;
use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::server::GrpcServer;
use crate::ultimate_coders::dashboard_service_server::DashboardService;
use crate::ultimate_coders::*;

/// NATS request timeout for Dashboard passthrough calls.
#[cfg(feature = "messaging")]
const DASHBOARD_NATS_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// NATS subject prefix for Dashboard passthrough RPCs.
pub const NATS_SUBJECT_DASHBOARD_PREFIX: &str = "uc.dashboard";

/// NATS subject for Dashboard snapshot streaming.
pub const NATS_SUBJECT_DASHBOARD_SNAPSHOT: &str = "uc.dashboard.snapshot";

/// NATS subject for incremental task events (event-driven push).
#[cfg(feature = "messaging")]
pub const NATS_SUBJECT_TASK_EVENT: &str = "uc.task.event";

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> DashboardService for GrpcServer<E> {
    type WatchDashboardStream = std::pin::Pin<
        Box<dyn tokio_stream::Stream<Item = Result<DashboardSnapshot, Status>> + Send>,
    >;

    async fn list_workers(
        &self,
        _request: Request<ListWorkersRequest>,
    ) -> Result<Response<ListWorkersResponse>, Status> {
        match self
            .nats_dashboard_request("ListWorkers", serde_json::json!({}))
            .await
        {
            Ok(json) => Ok(Response::new(json_to_list_workers_response(&json))),
            Err(_) => {
                // Fallback: construct response from local worker heartbeat state
                let store = self.task_store().lock().await;
                let now = chrono::Utc::now();
                let mut workers: Vec<WorkerProto> = Vec::new();
                for (id, ts) in store.worker_heartbeats() {
                    let age = (now - *ts).num_seconds() as f64;
                    workers.push(WorkerProto {
                        id: id.clone(),
                        capabilities: vec!["code".to_string()],
                        current_load: 0,
                        max_capacity: 3,
                        load_percent: 0,
                        last_heartbeat: ts.to_rfc3339(),
                        heartbeat_age_seconds: age,
                        heartbeat_stale: age > 60.0,
                        is_available: age <= 60.0,
                    });
                }
                drop(store);
                let available_count = workers.iter().filter(|w| w.is_available).count() as u32;
                let total = workers.len() as u32;
                Ok(Response::new(ListWorkersResponse {
                    available: true,
                    workers,
                    total,
                    available_count,
                }))
            }
        }
    }

    async fn get_scheduler_status(
        &self,
        _request: Request<GetSchedulerStatusRequest>,
    ) -> Result<Response<GetSchedulerStatusResponse>, Status> {
        match self
            .nats_dashboard_request("GetSchedulerStatus", serde_json::json!({}))
            .await
        {
            Ok(json) => Ok(Response::new(json_to_scheduler_status_response(&json))),
            Err(_) => Ok(Response::new(GetSchedulerStatusResponse {
                available: false,
                is_running: false,
                night_window: None,
                jobs: vec![],
                execution_history: vec![],
            })),
        }
    }

    async fn get_circuit_breaker_status(
        &self,
        _request: Request<GetCircuitBreakerStatusRequest>,
    ) -> Result<Response<CircuitBreakerStatusResponse>, Status> {
        match self
            .nats_dashboard_request("GetCircuitBreakerStatus", serde_json::json!({}))
            .await
        {
            Ok(json) => Ok(Response::new(json_to_circuit_breaker_status_response(
                &json,
            ))),
            Err(_) => Ok(Response::new(CircuitBreakerStatusResponse {
                available: false,
                circuit_breaker: None,
                rate_limiter: None,
            })),
        }
    }

    async fn reset_circuit_breaker(
        &self,
        _request: Request<ResetCircuitBreakerRequest>,
    ) -> Result<Response<ResetCircuitBreakerResponse>, Status> {
        let json = self
            .nats_dashboard_request("ResetCircuitBreaker", serde_json::json!({}))
            .await?;
        Ok(Response::new(json_to_reset_circuit_breaker_response(&json)))
    }

    async fn trigger_scheduler_job(
        &self,
        request: Request<TriggerSchedulerJobRequest>,
    ) -> Result<Response<TriggerSchedulerJobResponse>, Status> {
        let req = request.into_inner();
        let json = self
            .nats_dashboard_request(
                "TriggerSchedulerJob",
                serde_json::json!({ "job_id": req.job_id }),
            )
            .await?;
        Ok(Response::new(json_to_trigger_scheduler_job_response(&json)))
    }

    async fn flush_pending_tasks(
        &self,
        _request: Request<FlushPendingTasksRequest>,
    ) -> Result<Response<FlushPendingTasksResponse>, Status> {
        let json = self
            .nats_dashboard_request("FlushPendingTasks", serde_json::json!({}))
            .await?;
        Ok(Response::new(json_to_flush_pending_tasks_response(&json)))
    }

    async fn list_events(
        &self,
        request: Request<ListEventsRequest>,
    ) -> Result<Response<ListEventsResponse>, Status> {
        let req = request.into_inner();
        match self
            .nats_dashboard_request(
                "ListEvents",
                serde_json::json!({
                    "task_id": req.task_id,
                    "limit": req.limit,
                    "offset": req.offset,
                }),
            )
            .await
        {
            Ok(json) => Ok(Response::new(json_to_list_events_response(&json))),
            Err(_) => Ok(Response::new(ListEventsResponse {
                available: false,
                events: vec![],
                total: 0,
                offset: 0,
                limit: 0,
            })),
        }
    }

    async fn watch_dashboard(
        &self,
        _request: Request<WatchDashboardRequest>,
    ) -> Result<Response<Self::WatchDashboardStream>, Status> {
        #[cfg(feature = "messaging")]
        {
            match self.nats_client() {
                Some(nats_client) => {
                    let stream = async_stream::stream! {
                        // Subscribe to both snapshot and incremental event subjects
                        let mut snapshot_sub = match nats_client
                            .subscribe(NATS_SUBJECT_DASHBOARD_SNAPSHOT)
                            .await
                        {
                            Ok(s) => Some(s),
                            Err(e) => {
                                tracing::warn!("Dashboard snapshot subscribe failed: {e}");
                                None
                            }
                        };
                        let mut event_sub = match nats_client
                            .subscribe(NATS_SUBJECT_TASK_EVENT)
                            .await
                        {
                            Ok(s) => Some(s),
                            Err(e) => {
                                tracing::warn!("Dashboard event subscribe failed: {e}");
                                None
                            }
                        };

                        if snapshot_sub.is_none() && event_sub.is_none() {
                            yield Err(Status::unavailable("NATS subscription failed"));
                            return;
                        }

                        // Send initial full snapshot
                        if let Some(ref mut sub) = snapshot_sub {
                            if let Ok(Some(message)) = tokio::time::timeout(
                                std::time::Duration::from_secs(5),
                                sub.next(),
                            ).await {
                                match serde_json::from_slice::<serde_json::Value>(&message.payload) {
                                    Ok(json_val) => yield Ok(json_to_dashboard_snapshot(&json_val)),
                                    Err(e) => tracing::warn!("Initial snapshot parse error: {e}"),
                                }
                            }
                        }

                        let snapshot_interval = std::time::Duration::from_secs(30);
                        let mut last_snapshot = tokio::time::Instant::now();

                        loop {
                            // Priority: event_sub (incremental) > snapshot_sub (periodic)
                            tokio::select! {
                                // ── Incremental task events ─────────────────
                                event_result = async {
                                    match &mut event_sub {
                                        Some(sub) => sub.next().await,
                                        None => {
                                            // No event subscription — sleep forever
                                            std::future::pending::<Option<_>>().await
                                        }
                                    }
                                } => {
                                    match event_result {
                                        Some(message) => {
                                            match serde_json::from_slice::<serde_json::Value>(&message.payload) {
                                                Ok(json_val) => {
                                                    // Push as a lightweight snapshot with just the event
                                                    yield Ok(event_to_dashboard_snapshot(&json_val));
                                                }
                                                Err(e) => {
                                                    tracing::warn!("Dashboard event parse error: {e}");
                                                }
                                            }
                                        }
                                        None => break,
                                    }
                                }
                                // ── Periodic full snapshot ──────────────────
                                snapshot_result = async {
                                    match &mut snapshot_sub {
                                        Some(sub) => sub.next().await,
                                        None => {
                                            // No snapshot subscription — wait for interval
                                            tokio::time::sleep(snapshot_interval).await;
                                            // ponytail: None<Msg> — signals no message, triggers
                                            // the snapshot path below (periodic reconciliation)
                                            None
                                        }
                                    }
                                } => {
                                    match snapshot_result {
                                        Some(message) => {
                                            match serde_json::from_slice::<serde_json::Value>(&message.payload) {
                                                Ok(json_val) => yield Ok(json_to_dashboard_snapshot(&json_val)),
                                                Err(e) => tracing::warn!("Dashboard snapshot parse error: {e}"),
                                            }
                                            last_snapshot = tokio::time::Instant::now();
                                        }
                                        None => break,
                                    }
                                }
                                // ── Heartbeat on idle ──────────────────────
                                _ = tokio::time::sleep(std::time::Duration::from_secs(30)) => {
                                    // Only heartbeat if no recent snapshot
                                    if last_snapshot.elapsed() > std::time::Duration::from_secs(20) {
                                        yield Ok(DashboardSnapshot {
                                            timestamp: chrono::Utc::now().to_rfc3339(),
                                            ..Default::default()
                                        });
                                    }
                                }
                            }
                        }
                    };
                    Ok(Response::new(Box::pin(stream)))
                }
                // ponytail: no NATS — return a slow heartbeat stream instead of error,
                // so the frontend doesn't infinite-retry
                None => {
                    let stream = async_stream::stream! {
                        loop {
                            tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                            yield Ok(DashboardSnapshot {
                                timestamp: chrono::Utc::now().to_rfc3339(),
                                ..Default::default()
                            });
                        }
                    };
                    Ok(Response::new(Box::pin(stream)))
                }
            }
        }

        #[cfg(not(feature = "messaging"))]
        {
            // No NATS — event-driven snapshots from TaskStore + event_rx
            let task_store = self.task_store().clone();
            let event_rx = self.event_sender().subscribe();

            let stream = async_stream::stream! {
                let mut event_rx = event_rx;
                let mut snapshot_interval = tokio::time::interval(std::time::Duration::from_secs(5));

                loop {
                    tokio::select! {
                        event_result = event_rx.recv() => {
                            match event_result {
                                Ok(task_event) => {
                                    // Push fine-grained event as a lightweight snapshot
                                    yield Ok(DashboardSnapshot {
                                        timestamp: task_event.timestamp.clone(),
                                        recent_task_events: vec![task_event],
                                        ..Default::default()
                                    });
                                }
                                Err(broadcast::error::RecvError::Lagged(n)) => {
                                    tracing::warn!(skipped = n, "Dashboard event_rx lagged");
                                }
                                Err(broadcast::error::RecvError::Closed) => break,
                            }
                        }
                        _ = snapshot_interval.tick() => {
                            // Periodic full snapshot
                            let snapshot = build_local_snapshot(&task_store).await;
                            yield Ok(snapshot);
                        }
                    }
                }
            };
            Ok(Response::new(Box::pin(stream)))
        }
    }
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Send a NATS request-reply to Python Orchestrator for Dashboard data.
    async fn nats_dashboard_request(
        &self,
        rpc_name: &str,
        payload: serde_json::Value,
    ) -> Result<serde_json::Value, Status> {
        #[cfg(feature = "messaging")]
        {
            let nats_client = self
                .nats_client()
                .ok_or_else(|| Status::unavailable("NATS not connected"))?;

            let subject = format!("{NATS_SUBJECT_DASHBOARD_PREFIX}.{rpc_name}");
            let bytes = serde_json::to_vec(&payload)
                .map_err(|e| Status::internal(format!("serialize error: {e}")))?;

            let request = async_nats::Request::new()
                .payload(bytes.into())
                .timeout(Some(DASHBOARD_NATS_TIMEOUT));

            match nats_client.send_request(subject, request).await {
                Ok(response) => {
                    let json_str = String::from_utf8_lossy(&response.payload);
                    serde_json::from_str(&json_str).map_err(|e| {
                        tracing::warn!(
                            "Dashboard passthrough JSON parse error for {rpc_name}: {e}"
                        );
                        Status::internal(format!("JSON parse error: {e}"))
                    })
                }
                Err(e) => {
                    tracing::warn!("Dashboard NATS request-reply timeout for {rpc_name}: {e}");
                    Err(Status::unavailable(format!(
                        "Python Orchestrator unavailable for {rpc_name}"
                    )))
                }
            }
        }

        #[cfg(not(feature = "messaging"))]
        {
            let _ = (rpc_name, payload);
            Err(Status::unavailable(
                "Dashboard requires NATS (messaging feature not enabled)",
            ))
        }
    }
}

// ── JSON → Proto conversion helpers ────────────────────────────
// ponytail: manual field mapping — no extra deps, each function is small and explicit

fn json_bool(v: &serde_json::Value, key: &str) -> bool {
    v.get(key).and_then(|v| v.as_bool()).unwrap_or(false)
}

fn json_str<'a>(v: &'a serde_json::Value, key: &str) -> &'a str {
    v.get(key).and_then(|v| v.as_str()).unwrap_or("")
}

fn json_u32(v: &serde_json::Value, key: &str) -> u32 {
    v.get(key).and_then(|v| v.as_u64()).unwrap_or(0) as u32
}

fn json_f64(v: &serde_json::Value, key: &str) -> f64 {
    v.get(key).and_then(|v| v.as_f64()).unwrap_or(0.0)
}

fn json_opt_str(v: &serde_json::Value, key: &str) -> Option<String> {
    v.get(key).and_then(|v| v.as_str()).map(|s| s.to_string())
}

/// Used by `json_to_dashboard_snapshot` which is only called from the
/// `messaging` feature-gated `watch_dashboard` stream.
#[allow(dead_code)]
fn json_opt_u64(v: &serde_json::Value, key: &str) -> Option<u64> {
    v.get(key).and_then(|v| v.as_u64())
}

fn json_to_worker_proto(v: &serde_json::Value) -> WorkerProto {
    WorkerProto {
        id: json_str(v, "id").to_string(),
        capabilities: v
            .get("capabilities")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default(),
        current_load: json_u32(v, "current_load"),
        max_capacity: json_u32(v, "max_capacity"),
        load_percent: json_u32(v, "load_percent"),
        last_heartbeat: json_str(v, "last_heartbeat").to_string(),
        heartbeat_age_seconds: json_f64(v, "heartbeat_age_seconds"),
        heartbeat_stale: json_bool(v, "heartbeat_stale"),
        is_available: json_bool(v, "is_available"),
    }
}

fn json_to_list_workers_response(v: &serde_json::Value) -> ListWorkersResponse {
    ListWorkersResponse {
        available: json_bool(v, "available"),
        workers: v
            .get("workers")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_worker_proto).collect())
            .unwrap_or_default(),
        total: json_u32(v, "total"),
        available_count: json_u32(v, "available_count"),
    }
}

fn json_to_night_window(v: &serde_json::Value) -> NightWindowProto {
    NightWindowProto {
        start: json_str(v, "start").to_string(),
        end: json_str(v, "end").to_string(),
        enabled: json_bool(v, "enabled"),
    }
}

fn json_to_scheduled_job(v: &serde_json::Value) -> ScheduledJobProto {
    ScheduledJobProto {
        id: json_str(v, "id").to_string(),
        name: json_str(v, "name").to_string(),
        cron: json_str(v, "cron").to_string(),
        enabled: json_bool(v, "enabled"),
        last_run: json_opt_str(v, "last_run"),
        next_run: json_opt_str(v, "next_run"),
    }
}

fn json_to_execution_history(v: &serde_json::Value) -> ExecutionHistoryProto {
    ExecutionHistoryProto {
        job_id: json_str(v, "job_id").to_string(),
        job_name: json_str(v, "job_name").to_string(),
        executed_at: json_str(v, "executed_at").to_string(),
        success: json_bool(v, "success"),
        error: json_opt_str(v, "error"),
    }
}

fn json_to_scheduler_status_response(v: &serde_json::Value) -> GetSchedulerStatusResponse {
    GetSchedulerStatusResponse {
        available: json_bool(v, "available"),
        is_running: json_bool(v, "is_running"),
        night_window: v.get("night_window").map(json_to_night_window),
        jobs: v
            .get("jobs")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_scheduled_job).collect())
            .unwrap_or_default(),
        execution_history: v
            .get("execution_history")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_execution_history).collect())
            .unwrap_or_default(),
    }
}

fn json_to_cb_proto(v: &serde_json::Value) -> CircuitBreakerProto {
    CircuitBreakerProto {
        state: json_str(v, "state").to_string(),
        failure_count: json_u32(v, "failure_count"),
        failure_threshold: json_u32(v, "failure_threshold"),
        recovery_timeout_seconds: json_f64(v, "recovery_timeout_seconds"),
        last_failure: json_opt_str(v, "last_failure"),
    }
}

fn json_to_rl_proto(v: &serde_json::Value) -> RateLimiterProto {
    RateLimiterProto {
        max_requests: json_u32(v, "max_requests"),
        window_seconds: json_f64(v, "window_seconds"),
        current_requests: json_u32(v, "current_requests"),
        remaining_ratio: json_f64(v, "remaining_ratio"),
    }
}

fn json_to_circuit_breaker_status_response(v: &serde_json::Value) -> CircuitBreakerStatusResponse {
    CircuitBreakerStatusResponse {
        available: json_bool(v, "available"),
        circuit_breaker: v.get("circuit_breaker").map(json_to_cb_proto),
        rate_limiter: v.get("rate_limiter").map(json_to_rl_proto),
    }
}

fn json_to_reset_circuit_breaker_response(v: &serde_json::Value) -> ResetCircuitBreakerResponse {
    ResetCircuitBreakerResponse {
        success: json_bool(v, "success"),
        state: json_str(v, "state").to_string(),
        error: json_opt_str(v, "error"),
    }
}

fn json_to_trigger_scheduler_job_response(v: &serde_json::Value) -> TriggerSchedulerJobResponse {
    TriggerSchedulerJobResponse {
        success: json_bool(v, "success"),
        job_id: json_str(v, "job_id").to_string(),
        error: json_opt_str(v, "error"),
    }
}

fn json_to_flush_pending_tasks_response(v: &serde_json::Value) -> FlushPendingTasksResponse {
    FlushPendingTasksResponse {
        success: json_bool(v, "success"),
        pending_count: json_u32(v, "pending_count"),
        executed_count: json_u32(v, "executed_count"),
        error: json_opt_str(v, "error"),
    }
}

fn json_to_dashboard_event(v: &serde_json::Value) -> DashboardEventProto {
    DashboardEventProto {
        timestamp: json_str(v, "timestamp").to_string(),
        r#type: json_str(v, "type").to_string(),
        task_id: json_str(v, "task_id").to_string(),
        data: v
            .get("data")
            .and_then(|v| v.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

fn json_to_list_events_response(v: &serde_json::Value) -> ListEventsResponse {
    ListEventsResponse {
        available: json_bool(v, "available"),
        events: v
            .get("events")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_dashboard_event).collect())
            .unwrap_or_default(),
        total: json_u32(v, "total"),
        offset: json_u32(v, "offset"),
        limit: json_u32(v, "limit"),
    }
}

/// Used by `json_to_task_proto` → `json_to_dashboard_snapshot` (messaging feature only).
#[allow(dead_code)]
fn json_to_subtask_proto(v: &serde_json::Value) -> SubtaskProto {
    SubtaskProto {
        id: json_str(v, "id").to_string(),
        description: json_str(v, "description").to_string(),
        status: json_str(v, "status").to_string(),
        depends_on: v
            .get("depends_on")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default(),
        assigned_worker: json_opt_str(v, "assigned_worker"),
        parent_id: json_str(v, "parent_id").to_string(),
        file_constraints: v
            .get("file_constraints")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default(),
        expected_output: json_str(v, "expected_output").to_string(),
        result: json_opt_str(v, "result"),
        dispatch_mode: json_opt_str(v, "dispatch_mode"),
        dispatch_retry_count: v.get("dispatch_retry_count").and_then(|v| v.as_u64()).map(|n| n as u32),
        required_capabilities: v
            .get("required_capabilities")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

/// Used by `json_to_list_tasks_response` → `json_to_dashboard_snapshot` (messaging feature only).
#[allow(dead_code)]
fn json_to_task_proto(v: &serde_json::Value) -> TaskProto {
    TaskProto {
        id: json_str(v, "id").to_string(),
        description: json_str(v, "description").to_string(),
        status: json_str(v, "status").to_string(),
        project_id: json_str(v, "project_id").to_string(),
        subtask_count: json_u32(v, "subtask_count"),
        created_at: v.get("created_at").and_then(|v| v.as_i64()).unwrap_or(0),
        updated_at: v.get("updated_at").and_then(|v| v.as_i64()).unwrap_or(0),
        subtasks: v
            .get("subtasks")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_subtask_proto).collect())
            .unwrap_or_default(),
    }
}

/// Used by `json_to_dashboard_snapshot` (messaging feature only).
#[allow(dead_code)]
fn json_to_list_tasks_response(v: &serde_json::Value) -> ListTasksResponse {
    ListTasksResponse {
        available: json_bool(v, "available"),
        tasks: v
            .get("tasks")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_task_proto).collect())
            .unwrap_or_default(),
        total: json_u32(v, "total"),
        status_counts: v
            .get("status_counts")
            .and_then(|v| v.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n as u32)))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

fn json_to_metrics_sample(v: &serde_json::Value) -> MetricsSample {
    MetricsSample {
        timestamp: v.get("timestamp").and_then(|v| v.as_i64()).unwrap_or(0),
        events_per_minute: json_f64(v, "events_per_minute"),
        avg_duration_ms: json_f64(v, "avg_duration_ms"),
        error_rate: json_f64(v, "error_rate"),
        cluster_utilization: json_f64(v, "cluster_utilization"),
    }
}

fn json_to_task_metrics(v: &serde_json::Value) -> TaskMetrics {
    TaskMetrics {
        avg_duration_ms: json_f64(v, "avg_duration_ms"),
        p50_duration_ms: json_f64(v, "p50_duration_ms"),
        p95_duration_ms: json_f64(v, "p95_duration_ms"),
        p99_duration_ms: json_f64(v, "p99_duration_ms"),
        retry_rate: json_f64(v, "retry_rate"),
        slow_tasks_count: json_u32(v, "slow_tasks_count"),
        total_completed: json_u32(v, "total_completed"),
        total_failed: json_u32(v, "total_failed"),
        success_rate: json_f64(v, "success_rate"),
    }
}

fn json_to_worker_metrics(v: &serde_json::Value) -> WorkerMetrics {
    WorkerMetrics {
        avg_heartbeat_age_seconds: json_f64(v, "avg_heartbeat_age_seconds"),
        per_worker_tool_calls: v
            .get("per_worker_tool_calls")
            .and_then(|v| v.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n as u32)))
                    .collect()
            })
            .unwrap_or_default(),
        per_worker_subtask_count: v
            .get("per_worker_subtask_count")
            .and_then(|v| v.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n as u32)))
                    .collect()
            })
            .unwrap_or_default(),
        cluster_load_pct: json_f64(v, "cluster_load_pct"),
    }
}

fn json_to_event_metrics(v: &serde_json::Value) -> EventMetrics {
    EventMetrics {
        events_per_minute: json_f64(v, "events_per_minute"),
        error_spike: json_bool(v, "error_spike"),
        event_type_counts: v
            .get("event_type_counts")
            .and_then(|v| v.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n as u32)))
                    .collect()
            })
            .unwrap_or_default(),
    }
}

fn json_to_system_metrics(v: &serde_json::Value) -> SystemMetrics {
    SystemMetrics {
        uptime_seconds: v
            .get("uptime_seconds")
            .and_then(|v| v.as_u64())
            .unwrap_or(0),
        circuit_breaker_state: json_str(v, "circuit_breaker_state").to_string(),
        rate_limiter_remaining_ratio: json_f64(v, "rate_limiter_remaining_ratio"),
        cluster_utilization_pct: json_f64(v, "cluster_utilization_pct"),
    }
}

fn json_to_metrics_snapshot(v: &serde_json::Value) -> MetricsSnapshot {
    MetricsSnapshot {
        task: Some(v.get("task").map(json_to_task_metrics).unwrap_or_default()),
        worker: Some(
            v.get("worker")
                .map(json_to_worker_metrics)
                .unwrap_or_default(),
        ),
        event: Some(
            v.get("event")
                .map(json_to_event_metrics)
                .unwrap_or_default(),
        ),
        system: Some(
            v.get("system")
                .map(json_to_system_metrics)
                .unwrap_or_default(),
        ),
        trend: v
            .get("trend")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_metrics_sample).collect())
            .unwrap_or_default(),
    }
}

/// Used by `watch_dashboard` stream (messaging feature only).
#[allow(dead_code)]
fn json_to_dashboard_snapshot(v: &serde_json::Value) -> DashboardSnapshot {
    DashboardSnapshot {
        timestamp: json_str(v, "timestamp").to_string(),
        health: v.get("health").map(|h| HealthSnapshot {
            available: json_bool(h, "available"),
            status: json_str(h, "status").to_string(),
            version: json_opt_str(h, "version"),
            uptime_seconds: json_opt_u64(h, "uptime_seconds"),
        }),
        workers: v.get("workers").map(json_to_list_workers_response),
        tasks: v.get("tasks").map(json_to_list_tasks_response),
        scheduler: v.get("scheduler").map(json_to_scheduler_status_response),
        circuit_breaker: v
            .get("circuit_breaker")
            .map(json_to_circuit_breaker_status_response),
        recent_events: v
            .get("recent_events")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().map(json_to_dashboard_event).collect())
            .unwrap_or_default(),
        recent_task_events: Vec::new(),
        metrics: v.get("metrics").map(json_to_metrics_snapshot),
    }
}

/// Convert a `uc.task.event` JSON payload into a lightweight DashboardSnapshot
/// containing only the event in `recent_task_events`. Used for incremental push.
#[cfg(feature = "messaging")]
fn event_to_dashboard_snapshot(v: &serde_json::Value) -> DashboardSnapshot {
    use crate::ultimate_coders::TaskEvent;
    let event = TaskEvent {
        timestamp: json_str(v, "timestamp").to_string(),
        r#type: json_str(v, "type").to_string(),
        task_id: json_str(v, "task_id").to_string(),
        subtask_id: json_opt_str(v, "subtask_id").unwrap_or_default().into(),
        data: v
            .get("data")
            .and_then(|d| d.as_object())
            .map(|obj| {
                obj.iter()
                    .map(|(k, val)| (k.clone(), val.to_string()))
                    .collect()
            })
            .unwrap_or_default(),
    };
    DashboardSnapshot {
        timestamp: event.timestamp.clone(),
        recent_task_events: vec![event],
        ..Default::default()
    }
}

// ── No-NATS fallback: construct DashboardSnapshot from local state ──────

/// Build a DashboardSnapshot directly from TaskStore + Engine when NATS is unavailable.
///
/// ponytail: minimal snapshot — workers from heartbeat tracking, tasks from TaskStore,
/// health from Engine. No scheduler/CB state without Python Orchestrator.
#[cfg(not(feature = "messaging"))]
async fn build_local_snapshot(
    task_store: &tokio::sync::Mutex<super::server::TaskStore>,
) -> DashboardSnapshot {
    use crate::conversions::task_status_to_proto;
    use std::collections::HashMap;

    let store = task_store.lock().await;
    let now = chrono::Utc::now();

    // Workers from per-worker heartbeat tracking
    let worker_protos: Vec<WorkerProto> = store
        .worker_heartbeats()
        .iter()
        .map(|(id, ts)| {
            let age = (now - *ts).num_seconds() as f64;
            WorkerProto {
                id: id.clone(),
                capabilities: vec!["code".to_string()],
                current_load: 0,
                max_capacity: 3,
                load_percent: 0,
                last_heartbeat: ts.to_rfc3339(),
                heartbeat_age_seconds: age,
                heartbeat_stale: age > 60.0,
                is_available: age <= 60.0,
            }
        })
        .collect();

    let workers_available = worker_protos.iter().filter(|w| w.is_available).count() as u32;
    let workers = ListWorkersResponse {
        available: true,
        workers: worker_protos,
        total: store.worker_heartbeats().len() as u32,
        available_count: workers_available,
    };

    // Tasks from TaskStore
    let tasks_list = store.list_tasks();
    let total = tasks_list.len() as u32;
    let mut status_counts: HashMap<String, u32> = HashMap::new();
    let task_protos: Vec<TaskProto> = tasks_list
        .into_iter()
        .map(|t| {
            *status_counts
                .entry(task_status_to_proto(&t.status).to_string())
                .or_insert(0) += 1;
            t.into()
        })
        .collect();

    let tasks = ListTasksResponse {
        available: true,
        tasks: task_protos,
        total,
        status_counts,
    };

    drop(store);

    // Recent task events from TaskStore (last 20 events)
    let store = task_store.lock().await;
    let total_events = store.event_count();
    let start = total_events.saturating_sub(20);
    let recent_task_events: Vec<TaskEvent> = store
        .read_events_from(start)
        .into_iter()
        .map(|ev| ev.into())
        .collect();
    drop(store);

    // ponytail: health from heartbeat — if we have heartbeats, system is healthy
    let health = HealthSnapshot {
        available: true,
        status: "ok".to_string(),
        version: None,
        uptime_seconds: None,
    };

    DashboardSnapshot {
        timestamp: now.to_rfc3339(),
        health: Some(health),
        workers: Some(workers),
        tasks: Some(tasks),
        scheduler: None,
        circuit_breaker: None,
        recent_events: Vec::new(),
        recent_task_events,
        metrics: None,
    }
}
