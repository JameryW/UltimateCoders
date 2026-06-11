//! Unified error types for the engine.
//!
//! Both PyO3 and gRPC paths must produce the same error categories.
//! The `EngineError` enum serves as the shared contract; PyO3 maps to
//! Python exceptions, gRPC maps to tonic Status codes.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("Search failed: {0}")]
    SearchError(String),

    #[error("Index not found: {0}")]
    IndexError(String),

    #[error("Memory read error: {0}")]
    MemoryReadError(String),

    #[error("Memory write error: {0}")]
    MemoryWriteError(String),

    #[error("Indexing failed: {0}")]
    IndexingError(String),

    #[error("Connection error: {0}")]
    ConnectionError(String),

    #[error("Timeout: {0}")]
    TimeoutError(String),

    #[error("Rate limited: retry after {0}s")]
    RateLimited(u64),

    #[error("Conflict detected in {path}: {details}")]
    ConflictError { path: String, details: String },

    #[error("Task failed: {0}")]
    TaskError(String),

    #[error("Worker unavailable: {0}")]
    WorkerUnavailable(String),

    #[error("Sandbox error: {0}")]
    SandboxError(String),

    #[error("Configuration error: {0}")]
    ConfigError(String),

    #[error("Internal error: {0}")]
    InternalError(String),
}

impl EngineError {
    /// Whether this error is retryable (rate limit, timeout, connection).
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            EngineError::RateLimited(_)
                | EngineError::TimeoutError(_)
                | EngineError::ConnectionError(_)
        )
    }

    /// Whether this error suggests model fallback.
    pub fn should_fallback(&self) -> bool {
        matches!(
            self,
            EngineError::RateLimited(_) | EngineError::TimeoutError(_)
        )
    }
}
