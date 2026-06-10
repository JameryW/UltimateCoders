//! # UltimateCoders - Core Engine
//!
//! Implements the `EngineApi` trait with local (in-process) components.
//! Depends on uc-types for the trait and data definitions.

pub mod local;
pub mod indexer;
pub mod memory;
pub mod scheduler;
pub mod search;
pub mod git;
pub mod config;
pub mod metadata;
pub mod events;
pub mod checkpoint;
pub mod conflict;
pub mod rate_limiter;
pub mod circuit_breaker;

pub use local::LocalEngine;
pub use config::{EngineConfig, StorageConfig, MemoryConfig, EmbeddingConfig};
pub use indexer::IndexPipeline;
pub use indexer::semantic::{EmbeddingService, SemanticIndexer};
pub use search::HybridSearchEngine;
pub use search::SemanticSearchEngine;
pub use events::{EventStore, InMemoryEventStore, AgentEventType, RecordedEvent, TaskSnapshot, LineRange};
pub use checkpoint::{CheckpointManager, CheckpointConfig};
pub use conflict::{ConflictDetector, EditIntent, EditType, ConflictResult, ResolutionTier, MergeResult, ConflictMarker};
pub use conflict::merger::three_way_merge;
pub use rate_limiter::{
    LlmRateLimiter, LlmRateLimiterConfig, TokenBucket,
    RequestPriority, ModelFallbackChain, TaskComplexity,
};
pub use circuit_breaker::{CircuitBreaker, CircuitBreakerConfig, CircuitState, RetryPolicy};
