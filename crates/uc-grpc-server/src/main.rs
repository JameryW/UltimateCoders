//! UltimateCoders gRPC Server
//!
//! Standalone binary that starts a tonic gRPC server with LocalEngine.
//! Serves both EngineService and TaskService.

use uc_engine::{EngineConfig, LocalEngine};
use uc_grpc::server::GrpcServer;

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

    // Wrap in gRPC server
    let grpc_server = GrpcServer::new(engine);
    let (engine_service, task_service) = grpc_server.into_services();

    // Determine listen address
    let addr = std::env::var("UC_GRPC_ADDR")
        .unwrap_or_else(|_| "[::]:50051".to_string())
        .parse()?;

    tracing::info!("UltimateCoders gRPC server listening on {}", addr);

    Server::builder()
        .add_service(engine_service)
        .add_service(task_service)
        .serve(addr)
        .await?;

    Ok(())
}
