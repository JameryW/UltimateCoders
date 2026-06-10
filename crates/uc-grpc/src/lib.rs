//! gRPC server/client for remote engine access.
//!
//! Provides:
//! - `GrpcServer` — wraps an `EngineApi` implementor as a tonic service
//! - `GrpcEngineClient` — implements `EngineApi` by calling a remote gRPC server
//! - `conversions` — bidirectional type mapping between proto and uc-types

pub mod server;
pub mod client;
pub mod conversions;

/// Generated protobuf types and service definitions.
pub mod ultimate_coders {
    tonic::include_proto!("ultimate_coders");
}
