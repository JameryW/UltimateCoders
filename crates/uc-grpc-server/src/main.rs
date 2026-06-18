//! UltimateCoders gRPC Server
//!
//! Standalone binary that starts a tonic gRPC server with LocalEngine.
//! Serves EngineService, TaskService, and the standard gRPC Health service.
//!
//! When NATS is available (via `UC_NATS_URL` env var), TaskService will
//! publish task submissions to NATS for the Python Orchestrator to process.
//! When NATS is unavailable, falls back to local task decomposition.
//!
//! tonic-web is enabled for gRPC-Web browser support (unary + server-streaming).
//! CORS is configured to allow dashboard origins.

use uc_engine::{EngineConfig, LocalEngine};
use uc_grpc::server::{health_reporter, GrpcServer};

use tonic::transport::Server;
use tonic_web::GrpcWebLayer;
use tower_http::cors::{AllowOrigin, Any, CorsLayer};
use tracing_subscriber::EnvFilter;

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

    // Create gRPC server, optionally with NATS integration
    let grpc_server = match std::env::var("UC_NATS_URL") {
        Ok(nats_url) => {
            tracing::info!(nats_url = %nats_url, "Attempting NATS connection for TaskService");
            GrpcServer::with_nats(engine, &nats_url).await
        }
        Err(_) => {
            tracing::info!("No UC_NATS_URL set, TaskService using local decomposition");
            GrpcServer::new(engine)
        }
    };
    let (engine_service, task_service) = grpc_server.into_services();

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
                        tracing::warn!("UC_CORS_ORIGINS set but no valid origins parsed; no origins allowed");
                        CorsLayer::new()
                            .allow_methods(Any)
                            .allow_headers(Any)
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
                    tracing::info!("No CORS origins configured; set UC_CORS_ORIGINS or UC_CORS_MODE=dev");
                    CorsLayer::new()
                        .allow_methods(Any)
                        .allow_headers(Any)
                }
            }
        }
    };

    tracing::info!(
        "UltimateCoders gRPC server listening on {} (gRPC-Web enabled)",
        addr
    );

    Server::builder()
        // Accept both gRPC and gRPC-Web (HTTP/1.1) requests
        .accept_http1(true)
        .layer(GrpcWebLayer::new())
        .layer(cors)
        .add_service(engine_service)
        .add_service(task_service)
        .add_service(health_service)
        .serve(addr)
        .await?;

    Ok(())
}
