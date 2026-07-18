//! UltimateCoders gRPC Server
//!
//! Standalone binary that starts a tonic gRPC server with LocalEngine.
//! Serves EngineService, TaskService, and the standard gRPC Health service.
//!
//! When NATS is available (via `UC_NATS_URL` env var), TaskService will
//! publish task submissions to NATS for the Python Orchestrator to process.
//! When NATS is unavailable, falls back to local task decomposition.
//!
//! Task persistence can be configured via `UC_TASK_BACKEND`:
//! - `memory` (default): in-memory HashMap, no persistence across restarts
//! - `postgres`: PostgreSQL-backed, requires `UC_DATABASE_URL`
//!
//! tonic-web is enabled for gRPC-Web browser support (unary + server-streaming).
//! CORS is configured to allow dashboard origins.

use std::sync::Arc;
use uc_engine::repos_config::{build_index_requests, load_repos_config};
use uc_engine::{EngineConfig, LocalEngine};
use uc_grpc::server::{health_reporter, GrpcServer};
use uc_grpc::AuthInterceptor;
use uc_types::EngineApi;

use tonic::transport::Server;
use tonic_web::GrpcWebLayer;
use tower_http::cors::{AllowOrigin, Any, CorsLayer};
use tracing_subscriber::EnvFilter;

/// Create a TaskStoreBackend based on environment configuration.
///
/// - `UC_TASK_BACKEND=postgres` + `UC_DATABASE_URL`: PostgreSQL persistence
/// - Default: in-memory (no persistence across restarts)
async fn create_task_backend() -> (
    Arc<dyn uc_engine::TaskStoreBackend>,
    Arc<dyn uc_engine::EventStore>,
) {
    let event_store = Arc::new(uc_engine::InMemoryEventStore::new());

    match std::env::var("UC_TASK_BACKEND").as_deref() {
        #[cfg(feature = "storage")]
        Ok("postgres") => {
            let db_url = std::env::var("UC_DATABASE_URL").unwrap_or_else(|_| {
                tracing::warn!("UC_TASK_BACKEND=postgres but UC_DATABASE_URL not set, falling back to in-memory");
                String::new()
            });
            if db_url.is_empty() {
                tracing::warn!("Empty UC_DATABASE_URL, using in-memory task backend");
                return (Arc::new(uc_engine::InMemoryTaskBackend::new()), event_store);
            }
            match uc_engine::PostgresTaskBackend::new(&db_url).await {
                Ok(backend) => {
                    tracing::info!("TaskStore using PostgreSQL backend");
                    (Arc::new(backend), event_store)
                }
                Err(e) => {
                    tracing::warn!("PostgreSQL task backend failed: {}, using in-memory", e);
                    (Arc::new(uc_engine::InMemoryTaskBackend::new()), event_store)
                }
            }
        }
        #[cfg(not(feature = "storage"))]
        Ok("postgres") => {
            tracing::warn!(
                "UC_TASK_BACKEND=postgres but storage feature not enabled, using in-memory"
            );
            (Arc::new(uc_engine::InMemoryTaskBackend::new()), event_store)
        }
        _ => {
            tracing::info!("TaskStore using in-memory backend");
            (Arc::new(uc_engine::InMemoryTaskBackend::new()), event_store)
        }
    }
}

/// Load `uc.repos.yaml` and index all configured workspace repos into the engine.
///
/// Resolution order: `UC_REPOS_CONFIG` env, then `./uc.repos.yaml`, then
/// `./uc.repos.yml`, then skip (no error). Indexes explicit `repos` entries
/// (local_path only) and `scan_dirs` auto-discoveries, all tagged with the
/// config's `workspace_id`. Per-repo failures log a warning but do not abort
/// startup (tolerant, mirrors Python worker behavior).
async fn index_workspace_repos(engine: &LocalEngine) {
    let cfg = match load_repos_config(None) {
        Some(c) => c,
        None => {
            tracing::info!("No uc.repos.yaml found; skipping workspace repo indexing");
            return;
        }
    };
    let workspace_id = cfg.workspace_id.clone();
    let requests = build_index_requests(&cfg);
    if requests.is_empty() {
        tracing::info!(
            workspace_id = %workspace_id,
            "uc.repos.yaml loaded but no indexable repos (all remote-only or empty)"
        );
        return;
    }
    let total = requests.len();
    tracing::info!(
        workspace_id = %workspace_id,
        total = total,
        "Indexing workspace repos from uc.repos.yaml"
    );
    let mut ok = 0usize;
    for req in requests {
        let repo_id = req.repo.repo_id.clone();
        match engine.index_repo(req).await {
            Ok(resp) => {
                ok += 1;
                tracing::info!(
                    repo_id = %repo_id,
                    files = resp.files_indexed,
                    "Indexed workspace repo"
                );
            }
            Err(e) => {
                tracing::warn!(repo_id = %repo_id, error = %e, "Failed to index workspace repo");
            }
        }
    }
    tracing::info!(
        workspace_id = %workspace_id,
        indexed = ok,
        total = total,
        "Workspace repo indexing complete"
    );
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("info".parse()?))
        .init();

    // Load configuration from environment
    let config = EngineConfig::from_env();

    // Create local engine (with fallback to in-memory if storage is unavailable)
    let engine = match LocalEngine::new(config).await {
        Ok(e) => {
            tracing::info!("LocalEngine created with storage backends");
            e
        }
        Err(err) => {
            tracing::warn!(
                "Failed to create LocalEngine with storage: {}. Using fallback.",
                err
            );
            LocalEngine::new_fallback()
        }
    };

    // Load uc.repos.yaml and index configured workspace repos at startup.
    // Mirrors Python worker's load_repos_config + RepoScanner.discover_and_index.
    // ponytail: local-path repos only (remote-only entries are skipped — handled
    // by Python worker mode). Failures per-repo log a warning but don't abort.
    //
    // CRITICAL: indexing must NOT block Server::serve. A full reindex of a large
    // repo (e.g. UltimateCoders, 4905 files, triggered when its SHA is no longer
    // an ancestor of HEAD) can take minutes. If we `.await` here, port 50051
    // never binds until indexing finishes — the docker healthcheck marks the
    // container unhealthy, `restart: unless-stopped` does not restart unhealthy
    // (only exited) containers, and the process never exits (blocked in async),
    // leaving a permanent zombie that workers cannot connect to.
    //
    // Fix: clone the engine (zero-cost — LocalEngine is all-Arc, see
    // local.rs) and spawn indexing as a detached task. Server::serve binds
    // 50051 within seconds; indexing proceeds in the background. Searches
    // during indexing return partial results — acceptable, and strictly better
    // than a zombie gateway.
    let index_engine = engine.clone();
    tokio::spawn(async move {
        tracing::info!("Background workspace indexing started (non-blocking serve)");
        index_workspace_repos(&index_engine).await;
        tracing::info!("Background workspace indexing finished");
    });

    // Create task store backend (in-memory or PostgreSQL)
    let (task_backend, event_store) = create_task_backend().await;

    // Create gRPC server, optionally with NATS integration
    let grpc_server = match std::env::var("UC_NATS_URL") {
        Ok(nats_url) => {
            tracing::info!(nats_url = %nats_url, "Attempting NATS connection for TaskService");
            GrpcServer::with_nats_and_backends(engine, &nats_url, task_backend, event_store).await
        }
        Err(_) => {
            tracing::info!("No UC_NATS_URL set, TaskService using local decomposition");
            GrpcServer::with_backends(engine, task_backend, event_store)
        }
    };
    // Create health reporter (marks EngineService as serving)
    let (_reporter, health_service) = health_reporter::<LocalEngine>().await;

    // Determine listen address
    let addr = std::env::var("UC_GRPC_ADDR")
        .unwrap_or_else(|_| "[::]:50051".to_string())
        .parse()?;

    // CORS layer — allow dashboard origins for gRPC-Web browser requests
    // UC_CORS_MODE=dev: allow Any origins (local development only)
    // UC_CORS_ORIGINS: comma-separated list of allowed origins (production)
    // Default: restrictive (no origins allowed) — browsers block cross-origin requests
    let cors = match std::env::var("UC_CORS_MODE").as_deref() {
        Ok("dev") => {
            tracing::warn!("CORS running in dev mode — allowing any origin");
            CorsLayer::new()
                .allow_origin(Any)
                .allow_methods(Any)
                .allow_headers(Any)
        }
        _ => {
            // Production mode: only allow explicitly listed origins
            match std::env::var("UC_CORS_ORIGINS") {
                Ok(origins) if !origins.is_empty() => {
                    let allowed: Vec<_> = origins
                        .split(',')
                        .map(|o| o.trim())
                        .filter(|o| !o.is_empty())
                        .collect();
                    if allowed.is_empty() {
                        tracing::warn!(
                            "UC_CORS_ORIGINS set but no valid origins parsed; no origins allowed"
                        );
                        CorsLayer::new().allow_methods(Any).allow_headers(Any)
                    } else {
                        let parsed: Vec<_> = allowed
                            .iter()
                            .filter_map(|o| match o.parse() {
                                Ok(hv) => Some(hv),
                                Err(e) => {
                                    tracing::warn!("Invalid CORS origin '{}': {}", o, e);
                                    None
                                }
                            })
                            .collect();
                        CorsLayer::new()
                            .allow_origin(AllowOrigin::list(parsed))
                            .allow_methods(Any)
                            .allow_headers(Any)
                    }
                }
                _ => {
                    // No origins configured — restrictive by default
                    tracing::info!(
                        "No CORS origins configured; set UC_CORS_ORIGINS or UC_CORS_MODE=dev"
                    );
                    CorsLayer::new().allow_methods(Any).allow_headers(Any)
                }
            }
        }
    };

    // ── Auth interceptor (UC_DASHBOARD_TOKEN) ──────────────────────────
    // Non-empty token → wrap all 4 business services with AuthInterceptor so
    // every RPC requires `Authorization: Bearer <token>`. Empty/unset → no
    // wrapping, open access (backwards compat for dev / local). The standard
    // tonic_health service is never wrapped (kube/docker probe compatibility).
    let dashboard_token = std::env::var("UC_DASHBOARD_TOKEN")
        .unwrap_or_default()
        .trim()
        .to_string();

    tracing::info!(
        "UltimateCoders gRPC server listening on {} (gRPC-Web enabled)",
        addr
    );

    let server_builder = if dashboard_token.is_empty() {
        tracing::info!("gRPC auth disabled (no UC_DASHBOARD_TOKEN — open access)");

        let (engine_service, task_service, dashboard_service, worker_service) =
            grpc_server.into_services();

        Server::builder()
            // Accept both gRPC and gRPC-Web (HTTP/1.1) requests
            .accept_http1(true)
            .layer(GrpcWebLayer::new())
            .layer(cors)
            .add_service(engine_service)
            .add_service(task_service)
            .add_service(dashboard_service)
            .add_service(worker_service)
    } else {
        tracing::info!("gRPC auth enabled (UC_DASHBOARD_TOKEN set)");

        let interceptor = AuthInterceptor::new(Arc::from(dashboard_token.as_str()));
        let (engine_service, task_service, dashboard_service, worker_service) =
            grpc_server.into_intercepted_services(interceptor);

        Server::builder()
            // Accept both gRPC and gRPC-Web (HTTP/1.1) requests
            .accept_http1(true)
            .layer(GrpcWebLayer::new())
            .layer(cors.clone())
            .add_service(engine_service)
            .add_service(task_service)
            .add_service(dashboard_service)
            .add_service(worker_service)
    };

    // health_service is added unconditionally OUTSIDE the auth gate so kube /
    // docker health probes keep working without a bearer token.
    server_builder
        .add_service(health_service)
        .serve(addr)
        .await?;

    Ok(())
}
