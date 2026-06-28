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
        age <= stale_timeout_secs
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
}
