//! # UltimateCoders - Core Engine
//!
//! Implements the `EngineApi` trait with local (in-process) components.
//! Depends on uc-types for the trait and data definitions.

pub mod checkpoint;
pub mod circuit_breaker;
pub mod config;
pub mod conflict;
pub mod events;
pub mod git;
pub mod indexer;
pub mod local;
pub mod memory;
pub mod metadata;
pub mod rate_limiter;
pub mod sandbox;
pub mod scheduler;
pub mod search;
pub mod task_store;

pub use checkpoint::{CheckpointConfig, CheckpointManager};
pub use circuit_breaker::{CircuitBreaker, CircuitBreakerConfig, CircuitState, RetryPolicy};
pub use config::{EmbeddingConfig, EngineConfig, MemoryConfig, StorageConfig};
pub use conflict::merger::three_way_merge;
pub use conflict::{
    ConflictDetector, ConflictMarker, ConflictResult, EditIntent, EditType, MergeResult,
    ResolutionTier,
};
pub use events::{
    AgentEventType, EventStore, InMemoryEventStore, LineRange, RecordedEvent, TaskSnapshot,
};
pub use indexer::semantic::{EmbeddingService, SemanticIndexer};
pub use indexer::IndexPipeline;
pub use local::LocalEngine;
pub use rate_limiter::{
    LlmRateLimiter, LlmRateLimiterConfig, ModelFallbackChain, RequestPriority, TaskComplexity,
    TokenBucket,
};
pub use search::HybridSearchEngine;
pub use search::SemanticSearchEngine;

// Sandbox module re-exports
pub use sandbox::agents::claude_code::ClaudeCodeAgent;
pub use sandbox::agents::codex::CodexAgent;
pub use sandbox::agents::{available_agents, create_adapter, AgentAdapter};
pub use sandbox::file_tracker::FileTracker;
pub use sandbox::pool::SandboxPool;
pub use sandbox::subprocess::SubprocessSandbox;
pub use sandbox::{
    AgentOutput, ExecRequest, ExecResult, NetworkMode, ResourceLimits, Sandbox, SandboxConfig,
    SandboxHandle, SandboxHealth, SandboxStatus, TokenUsage,
};

// Scheduler module re-exports
pub use scheduler::{
    AddJobResult, InMemoryScheduleStore, LoggingDispatcher, NightWindow, NightWindowError,
    OrchestratorDispatcher, ScheduleDispatcher, ScheduleStore, SchedulerService, WindowEventType,
};

#[cfg(feature = "storage")]
pub use scheduler::PostgresScheduleStore;

#[cfg(feature = "messaging")]
pub use scheduler::publish_window_event;

#[cfg(feature = "docker")]
pub use sandbox::docker::DockerSandbox;
