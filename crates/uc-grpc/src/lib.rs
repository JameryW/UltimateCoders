//! gRPC server/client for remote engine access.
//!
//! Provides:
//! - `GrpcServer` — wraps an `EngineApi` implementor as a tonic service (EngineService + TaskService)
//! - `TaskStore` — in-memory task store for TaskService
//! - `GrpcEngineClient` — implements `EngineApi` by calling a remote gRPC server
//! - `conversions` — bidirectional type mapping between proto and uc-types

pub mod client;
pub mod conversions;
pub mod local_worker;
pub mod server;

pub use server::TaskStore;
pub use server::{
    NatsHeartbeat, NatsSubtaskUpdate, NatsTaskEvent, NatsTaskSubmit, NatsTaskUpdate,
    NATS_SUBJECT_HEARTBEAT, NATS_SUBJECT_TASK_EVENT, NATS_SUBJECT_TASK_SUBMIT,
    NATS_SUBJECT_TASK_UPDATE,
};

/// Generated protobuf types and service definitions.
pub mod ultimate_coders {
    tonic::include_proto!("ultimate_coders");
}
