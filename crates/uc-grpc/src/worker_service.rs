//! WorkerService implementation — Worker registration and lifecycle management.
//!
//! Workers call RegisterWorker on startup, WorkerHeartbeat periodically,
//! and DeregisterWorker on graceful shutdown. The gateway maintains an
//! in-memory WorkerRegistry as the source of truth for worker state.

use std::collections::HashMap;

use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::server::GrpcServer;
use crate::ultimate_coders::worker_service_server::WorkerService;
use crate::ultimate_coders::*;

// ── WorkerRegistry ───────────────────────────────────────────────

/// In-memory registry of connected workers.
///
/// Tracks worker capabilities, load, and heartbeat state.
/// This is the gateway's source of truth for worker discovery,
/// supplementing (and eventually replacing) NATS-based heartbeat tracking.
pub struct WorkerRegistry {
    workers: HashMap<String, RegisteredWorker>,
}

/// A worker that has registered with the gateway.
#[derive(Debug, Clone)]
pub struct RegisteredWorker {
    pub id: String,
    pub capabilities: Vec<String>,
    pub max_capacity: u32,
    pub current_load: u32,
    pub metadata: String,
    pub registered_at: chrono::DateTime<chrono::Utc>,
    pub last_heartbeat: chrono::DateTime<chrono::Utc>,
}

impl RegisteredWorker {
    /// Whether the worker is considered available (heartbeat not stale).
    pub fn is_available(&self, stale_timeout_secs: f64) -> bool {
        let age = (chrono::Utc::now() - self.last_heartbeat).num_seconds() as f64;
        // "available" means the worker can take more work: heartbeat fresh
        // AND under capacity. Without the capacity check, a saturated worker
        // (or one with max_capacity==0) was returned as available, causing
        // dispatch_ready_subtasks to over-assign to it.
        age <= stale_timeout_secs && self.current_load < self.max_capacity
    }

    /// Load as a percentage of max capacity.
    pub fn load_percent(&self) -> u32 {
        if self.max_capacity == 0 {
            return 100;
        }
        (self.current_load * 100) / self.max_capacity
    }
}

impl WorkerRegistry {
    /// Stale timeout in seconds — workers not heartbeating within this are considered unavailable.
    const STALE_TIMEOUT_SECS: f64 = 60.0;

    pub fn new() -> Self {
        Self {
            workers: HashMap::new(),
        }
    }

    /// Register a new worker or re-register an existing one.
    pub fn register(
        &mut self,
        worker_id: String,
        capabilities: Vec<String>,
        max_capacity: u32,
        metadata: String,
    ) -> Result<(), String> {
        if worker_id.is_empty() {
            return Err("worker_id cannot be empty".to_string());
        }
        let now = chrono::Utc::now();
        let worker = RegisteredWorker {
            id: worker_id.clone(),
            capabilities,
            max_capacity,
            current_load: 0,
            metadata,
            registered_at: now,
            last_heartbeat: now,
        };
        let is_reregister = self.workers.contains_key(&worker_id);
        self.workers.insert(worker_id.clone(), worker);
        if is_reregister {
            tracing::info!(worker_id = %worker_id, "Worker re-registered");
        } else {
            tracing::info!(worker_id = %worker_id, "Worker registered");
        }
        Ok(())
    }

    /// Process a heartbeat from a worker.
    pub fn heartbeat(&mut self, worker_id: &str, current_load: u32) -> Result<(), String> {
        let worker = self.workers.get_mut(worker_id).ok_or_else(|| {
            format!(
                "Worker '{}' not registered — call RegisterWorker first",
                worker_id
            )
        })?;
        worker.last_heartbeat = chrono::Utc::now();
        worker.current_load = current_load;
        Ok(())
    }

    /// Deregister a worker (graceful shutdown).
    pub fn deregister(&mut self, worker_id: &str) -> Result<(), String> {
        if self.workers.remove(worker_id).is_some() {
            tracing::info!(worker_id = %worker_id, "Worker deregistered");
            Ok(())
        } else {
            Err(format!("Worker '{}' not found", worker_id))
        }
    }

    /// Get all registered workers.
    pub fn workers(&self) -> &HashMap<String, RegisteredWorker> {
        &self.workers
    }

    /// Get available workers (heartbeat not stale, has capacity).
    pub fn available_workers(&self) -> Vec<&RegisteredWorker> {
        self.workers
            .values()
            .filter(|w| w.is_available(Self::STALE_TIMEOUT_SECS))
            .collect()
    }

    /// Find workers that have ALL the specified capabilities.
    pub fn workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker> {
        let required_set: std::collections::HashSet<_> = required.iter().collect();
        self.available_workers()
            .into_iter()
            .filter(|w| {
                let worker_caps: std::collections::HashSet<_> = w.capabilities.iter().collect();
                required_set.is_subset(&worker_caps)
            })
            .collect()
    }

    /// Mark workers with stale heartbeats as unavailable (returns stale worker IDs).
    pub fn stale_worker_ids(&self) -> Vec<String> {
        self.workers
            .values()
            .filter(|w| !w.is_available(Self::STALE_TIMEOUT_SECS))
            .map(|w| w.id.clone())
            .collect()
    }

    /// Convert to proto WorkerProto list for ListWorkers response.
    pub fn to_worker_protos(&self) -> Vec<WorkerProto> {
        let now = chrono::Utc::now();
        self.workers
            .values()
            .map(|w| {
                let age = (now - w.last_heartbeat).num_seconds() as f64;
                let available = w.is_available(Self::STALE_TIMEOUT_SECS);
                WorkerProto {
                    id: w.id.clone(),
                    capabilities: w.capabilities.clone(),
                    current_load: w.current_load,
                    max_capacity: w.max_capacity,
                    load_percent: w.load_percent(),
                    last_heartbeat: w.last_heartbeat.to_rfc3339(),
                    heartbeat_age_seconds: age,
                    heartbeat_stale: !available,
                    is_available: available,
                }
            })
            .collect()
    }
}

impl Default for WorkerRegistry {
    fn default() -> Self {
        Self::new()
    }
}

// ── WorkerService gRPC implementation ─────────────────────────────

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> WorkerService for GrpcServer<E> {
    async fn register_worker(
        &self,
        request: Request<RegisterWorkerRequest>,
    ) -> Result<Response<RegisterWorkerResponse>, Status> {
        let req = request.into_inner();
        if req.worker_id.is_empty() {
            return Ok(Response::new(RegisterWorkerResponse {
                success: false,
                worker_id: String::new(),
                error: Some("worker_id cannot be empty".to_string()),
            }));
        }

        let mut registry = self.worker_registry().write().await;
        match registry.register(
            req.worker_id.clone(),
            req.capabilities,
            req.max_capacity,
            req.metadata,
        ) {
            Ok(()) => Ok(Response::new(RegisterWorkerResponse {
                success: true,
                worker_id: req.worker_id,
                error: None,
            })),
            Err(e) => Ok(Response::new(RegisterWorkerResponse {
                success: false,
                worker_id: req.worker_id,
                error: Some(e),
            })),
        }
    }

    async fn worker_heartbeat(
        &self,
        request: Request<WorkerHeartbeatRequest>,
    ) -> Result<Response<WorkerHeartbeatResponse>, Status> {
        let req = request.into_inner();
        let mut registry = self.worker_registry().write().await;
        match registry.heartbeat(&req.worker_id, req.current_load) {
            Ok(()) => Ok(Response::new(WorkerHeartbeatResponse {
                accepted: true,
                error: None,
            })),
            Err(e) => Ok(Response::new(WorkerHeartbeatResponse {
                accepted: false,
                error: Some(e),
            })),
        }
    }

    async fn deregister_worker(
        &self,
        request: Request<DeregisterWorkerRequest>,
    ) -> Result<Response<DeregisterWorkerResponse>, Status> {
        let req = request.into_inner();
        let mut registry = self.worker_registry().write().await;
        match registry.deregister(&req.worker_id) {
            Ok(()) => Ok(Response::new(DeregisterWorkerResponse {
                success: true,
                error: None,
            })),
            Err(e) => Ok(Response::new(DeregisterWorkerResponse {
                success: false,
                error: Some(e),
            })),
        }
    }

    async fn scale_workers(
        &self,
        request: Request<ScaleWorkersRequest>,
    ) -> Result<Response<ScaleWorkersResponse>, Status> {
        let req = request.into_inner();
        tracing::info!(
            action = %req.action,
            target_count = req.target_count,
            worker_id = %req.worker_id,
            "ScaleWorkers request received"
        );

        match req.action.as_str() {
            "deregister" => {
                if req.worker_id.is_empty() {
                    return Ok(Response::new(ScaleWorkersResponse {
                        success: false,
                        error: Some("worker_id is required for action='deregister'".to_string()),
                        actual_count: 0,
                        message: String::new(),
                    }));
                }
                let mut registry = self.worker_registry().write().await;
                let worker_id = req.worker_id.clone();
                match registry.deregister(&worker_id) {
                    Ok(()) => {
                        let actual = registry.workers().len() as u32;
                        tracing::info!(worker_id = %worker_id, actual_count = actual, "Worker force-deregistered via ScaleWorkers");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: true,
                            error: None,
                            actual_count: actual,
                            message: format!("Worker '{}' deregistered", worker_id),
                        }))
                    }
                    Err(e) => {
                        let actual = registry.workers().len() as u32;
                        tracing::warn!(worker_id = %worker_id, error = %e, "ScaleWorkers deregister failed");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: false,
                            error: Some(e),
                            actual_count: actual,
                            message: String::new(),
                        }))
                    }
                }
            }
            "scale" => {
                // Shell out to docker compose to set the worker instance count.
                // --no-deps is MANDATORY: worker depends_on gateway, and the gateway
                // itself is issuing this command (would deadlock without --no-deps).
                let compose_file = std::env::var("UC_COMPOSE_FILE")
                    .unwrap_or_else(|_| "/app/docker/docker-compose.yml".to_string());
                let compose_project =
                    std::env::var("UC_COMPOSE_PROJECT").unwrap_or_else(|_| "docker".to_string());

                // Validate compose file exists before shelling out.
                let compose_path = std::path::Path::new(&compose_file);
                if !compose_path.exists() {
                    tracing::warn!(compose_file = %compose_file, "Compose file not found");
                    return Ok(Response::new(ScaleWorkersResponse {
                        success: false,
                        error: Some(format!(
                            "Compose file not found: '{}' (set UC_COMPOSE_FILE to the correct path)",
                            compose_file
                        )),
                        actual_count: 0,
                        message: String::new(),
                    }));
                }

                let target = req.target_count;
                tracing::info!(
                    compose_file = %compose_file,
                    compose_project = %compose_project,
                    target_count = target,
                    "Scaling workers via docker compose"
                );

                let output = tokio::process::Command::new("docker")
                    .arg("compose")
                    .arg("-p")
                    .arg(&compose_project)
                    .arg("-f")
                    .arg(&compose_file)
                    .arg("up")
                    .arg("-d")
                    .arg("--no-deps")
                    .arg("--scale")
                    .arg(format!("worker={}", target))
                    .arg("worker")
                    .output()
                    .await;

                match output {
                    Ok(output) if output.status.success() => {
                        // Workers self-register asynchronously on container start;
                        // the registry reconciles via the existing RegisterWorker path.
                        // We return the requested target as actual_count without blocking.
                        tracing::info!(target_count = target, "docker compose scale succeeded");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: true,
                            error: None,
                            actual_count: target,
                            message: format!(
                                "Scaled worker instances to {}; workers self-register asynchronously",
                                target
                            ),
                        }))
                    }
                    Ok(output) => {
                        // Non-zero exit — capture stderr for diagnostics.
                        let stderr = String::from_utf8_lossy(&output.stderr);
                        let stdout = String::from_utf8_lossy(&output.stdout);
                        let snippet = if !stderr.trim().is_empty() {
                            stderr.lines().take(5).collect::<Vec<_>>().join("; ")
                        } else {
                            stdout.lines().take(5).collect::<Vec<_>>().join("; ")
                        };
                        tracing::warn!(
                            exit_code = ?output.status.code(),
                            snippet = %snippet,
                            "docker compose scale failed"
                        );
                        Ok(Response::new(ScaleWorkersResponse {
                            success: false,
                            error: Some(format!(
                                "docker compose scale failed (exit {:?}): {}",
                                output.status.code(),
                                snippet
                            )),
                            actual_count: 0,
                            message: String::new(),
                        }))
                    }
                    Err(e) => {
                        // docker CLI not found or cannot spawn.
                        tracing::warn!(error = %e, "Failed to invoke docker CLI");
                        Ok(Response::new(ScaleWorkersResponse {
                            success: false,
                            error: Some(format!(
                                "Failed to invoke docker CLI: {} (ensure docker is installed and docker.sock is mounted)",
                                e
                            )),
                            actual_count: 0,
                            message: String::new(),
                        }))
                    }
                }
            }
            other => {
                tracing::warn!(action = other, "Unknown ScaleWorkers action");
                Ok(Response::new(ScaleWorkersResponse {
                    success: false,
                    error: Some(format!(
                        "Unknown action: '{}' (expected 'scale' or 'deregister')",
                        other
                    )),
                    actual_count: 0,
                    message: String::new(),
                }))
            }
        }
    }
}

// ── Tests ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_register_and_lookup() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["python".to_string(), "docker".to_string()],
            5,
            String::new(),
        )
        .unwrap();
        assert!(reg.workers().contains_key("w-1"));
        let w = &reg.workers()["w-1"];
        assert_eq!(w.capabilities, vec!["python", "docker"]);
        assert_eq!(w.max_capacity, 5);
    }

    #[test]
    fn registry_rejects_empty_id() {
        let mut reg = WorkerRegistry::new();
        assert!(reg
            .register(String::new(), vec![], 1, String::new())
            .is_err());
    }

    #[test]
    fn registry_heartbeat_updates_load() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        reg.heartbeat("w-1", 2).unwrap();
        assert_eq!(reg.workers()["w-1"].current_load, 2);
    }

    // Regression: is_available used to check only heartbeat staleness, so a
    // worker at full load (or max_capacity==0) was "available" and got
    // over-assigned. Now it also requires current_load < max_capacity.
    #[test]
    fn is_available_excludes_saturated_and_zero_capacity_workers() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "full".to_string(),
            vec!["code".to_string()],
            2, // max_capacity
            String::new(),
        )
        .unwrap();
        reg.heartbeat("full", 2).unwrap(); // load == capacity → saturated
        assert!(
            !reg.workers()["full"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "saturated worker must not be available"
        );

        reg.register(
            "zero".to_string(),
            vec!["code".to_string()],
            0, // max_capacity == 0 → can never take work
            String::new(),
        )
        .unwrap();
        reg.heartbeat("zero", 0).unwrap();
        assert!(
            !reg.workers()["zero"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "zero-capacity worker must not be available"
        );

        // A worker with spare capacity is available.
        reg.register(
            "spare".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        reg.heartbeat("spare", 1).unwrap();
        assert!(
            reg.workers()["spare"].is_available(WorkerRegistry::STALE_TIMEOUT_SECS),
            "worker with spare capacity should be available"
        );

        // available_workers / workers_with_capabilities reflect the filter.
        let avail: Vec<&str> = reg
            .available_workers()
            .iter()
            .map(|w| w.id.as_str())
            .collect();
        assert!(avail.contains(&"spare"));
        assert!(!avail.contains(&"full"));
        assert!(!avail.contains(&"zero"));
    }

    #[test]
    fn registry_heartbeat_unknown_worker() {
        let mut reg = WorkerRegistry::new();
        assert!(reg.heartbeat("unknown", 0).is_err());
    }

    #[test]
    fn registry_deregister() {
        let mut reg = WorkerRegistry::new();
        reg.register("w-1".to_string(), vec![], 1, String::new())
            .unwrap();
        reg.deregister("w-1").unwrap();
        assert!(!reg.workers().contains_key("w-1"));
    }

    #[test]
    fn registry_deregister_unknown() {
        let mut reg = WorkerRegistry::new();
        assert!(reg.deregister("unknown").is_err());
    }

    #[test]
    fn registry_workers_with_capabilities() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-rust".to_string(),
            vec!["rust".to_string(), "docker".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        reg.register(
            "w-python".to_string(),
            vec!["python".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        reg.register(
            "w-both".to_string(),
            vec!["rust".to_string(), "python".to_string()],
            3,
            String::new(),
        )
        .unwrap();

        let rust_workers = reg.workers_with_capabilities(&["rust".to_string()]);
        assert_eq!(rust_workers.len(), 2);

        let both = reg.workers_with_capabilities(&["rust".to_string(), "python".to_string()]);
        assert_eq!(both.len(), 1);
        assert_eq!(both[0].id, "w-both");
    }

    #[test]
    fn registry_to_worker_protos() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["code".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        let protos = reg.to_worker_protos();
        assert_eq!(protos.len(), 1);
        assert_eq!(protos[0].id, "w-1");
        assert_eq!(protos[0].capabilities, vec!["code"]);
        assert!(protos[0].is_available);
    }

    #[test]
    fn registry_reregister_resets_state() {
        let mut reg = WorkerRegistry::new();
        reg.register(
            "w-1".to_string(),
            vec!["python".to_string()],
            3,
            String::new(),
        )
        .unwrap();
        reg.heartbeat("w-1", 2).unwrap();
        // Re-register with new capabilities
        reg.register(
            "w-1".to_string(),
            vec!["rust".to_string()],
            5,
            String::new(),
        )
        .unwrap();
        let w = &reg.workers()["w-1"];
        assert_eq!(w.capabilities, vec!["rust"]);
        assert_eq!(w.max_capacity, 5);
        assert_eq!(w.current_load, 0); // reset on re-register
    }

    // ── ScaleWorkers handler tests ──────────────────────────────────

    use crate::server::GrpcServer;
    use tonic::Request;
    use uc_engine::LocalEngine;

    fn make_server() -> GrpcServer<LocalEngine> {
        GrpcServer::new(LocalEngine::new_fallback())
    }

    #[tokio::test]
    async fn scale_workers_deregister_removes_worker() {
        let server = make_server();
        // Seed the registry with a worker via the register RPC.
        server
            .register_worker(Request::new(RegisterWorkerRequest {
                worker_id: "w-scale-1".to_string(),
                capabilities: vec!["python".to_string()],
                max_capacity: 2,
                metadata: String::new(),
            }))
            .await
            .unwrap();

        // Pre-condition: registry has 1 worker.
        {
            let reg = server.worker_registry().read().await;
            assert_eq!(reg.workers().len(), 1);
        }

        // Force-deregister via ScaleWorkers action="deregister".
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: "w-scale-1".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(resp.success, "deregister should succeed: {:?}", resp.error);
        assert_eq!(resp.actual_count, 0);
        assert!(resp.message.contains("w-scale-1"));

        // Registry no longer contains the worker.
        let reg = server.worker_registry().read().await;
        assert!(!reg.workers().contains_key("w-scale-1"));
    }

    #[tokio::test]
    async fn scale_workers_deregister_unknown_worker_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: "ghost-worker".to_string(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("ghost-worker"));
    }

    #[tokio::test]
    async fn scale_workers_deregister_empty_id_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "deregister".to_string(),
                target_count: 0,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("worker_id"));
    }

    #[tokio::test]
    async fn scale_workers_scale_error_paths() {
        // These tests exercise the scale action's error handling WITHOUT a real
        // docker daemon. They are combined into one test to avoid env-var races
        // (UC_COMPOSE_FILE is process-global; parallel tests would contend).

        let server = make_server();

        // ── Case 1: compose file does not exist ──
        std::env::set_var("UC_COMPOSE_FILE", "/nonexistent/uc-test-compose-12345.yml");
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "scale".to_string(),
                target_count: 3,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(
            !resp.success,
            "scale should fail without compose file: {:?}",
            resp.message
        );
        let err = resp.error.unwrap();
        assert!(
            err.contains("Compose file not found"),
            "error should mention missing compose file, got: {}",
            err
        );

        // ── Case 2: compose file exists but is invalid (/dev/null) ──
        // Either docker CLI is missing (invoke error) or compose fails parsing.
        // Both must produce success=false without panicking.
        std::env::set_var("UC_COMPOSE_FILE", "/dev/null");
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "scale".to_string(),
                target_count: 1,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(
            !resp.success,
            "scale with /dev/null compose should not succeed: {:?}",
            resp.message
        );
        let err = resp.error.unwrap();
        assert!(
            err.contains("docker") || err.contains("compose") || err.contains("exit"),
            "error should reference docker/compose, got: {}",
            err
        );

        // Restore default so other tests are unaffected.
        std::env::remove_var("UC_COMPOSE_FILE");
    }

    #[tokio::test]
    async fn scale_workers_unknown_action_fails() {
        let server = make_server();
        let resp = server
            .scale_workers(Request::new(ScaleWorkersRequest {
                action: "bogus".to_string(),
                target_count: 0,
                worker_id: String::new(),
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!resp.success);
        assert!(resp.error.as_ref().unwrap().contains("Unknown action"));
    }
}
