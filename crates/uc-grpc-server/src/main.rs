//! UltimateCoders gRPC Server
//!
//! Standalone binary that starts a tonic gRPC server with LocalEngine.
//! Serves EngineService, TaskService, and the standard gRPC Health service.
//!
//! When NATS is available (via `UC_NATS_URL` env var), TaskService will
//! publish task submissions to NATS for the Python Orchestrator to process.
//! When NATS is unavailable, falls back to local task decomposition.

use uc_engine::{EngineConfig, LocalEngine};
use uc_grpc::server::{health_reporter, GrpcServer};

use tonic::transport::Server;
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
            GrpcServer::new(engine).await
        }
    };
    let (engine_service, task_service) = grpc_server.into_services();

    // Create health reporter (marks EngineService as serving)
    let (_reporter, health_service) = health_reporter::<LocalEngine>().await;

    // Determine listen address
    let addr = std::env::var("UC_GRPC_ADDR")
        .unwrap_or_else(|_| "[::]:50051".to_string())
        .parse()?;

    tracing::info!("UltimateCoders gRPC server listening on {}", addr);

    Server::builder()
        .add_service(engine_service)
        .add_service(task_service)
        .add_service(health_service)
        .serve(addr)
        .await?;

    Ok(())
}
