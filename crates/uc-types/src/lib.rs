//! # UltimateCoders - Shared Types
//!
//! Core type definitions shared across all crates.
//! No I/O, no framework dependencies — just data types and the EngineApi trait.

pub mod agent;
pub mod engine;
pub mod envelope;
pub mod error;
pub mod graph;
pub mod index;
pub mod memory;
pub mod scheduler;
pub mod search;

// Re-exports for convenience
pub use agent::{
    AgentEvent, AgentEventPayload, ChangeType, DispatchMode, EffectClass, FileChange, Subtask,
    SubtaskResult, SubtaskSnapshot, SubtaskStatus, Task, TaskId, TaskSnapshot, TaskStatus,
    WorkerId, WorkerInfo, WorkflowStep,
};
pub use async_trait::async_trait;
pub use engine::{ComponentHealth, EngineApi, HealthStatus, RepoIndexState, SearchStream};
pub use envelope::{ExecutionEnvelope, CONTRACT_VERSION};
pub use error::EngineError;
pub use graph::{can_transition, NodeStatus};
pub use index::{
    ChunkType, CodeChunk, IndexHealth, IndexRequest, IndexResponse, IndexState, RepoSpec,
};
pub use memory::{
    MemoryContent, MemoryEntry, MemoryId, MemoryKey, MemoryMetadata, MemoryReadRequest,
    MemorySearchRequest, MemorySearchResponse, MemorySearchResult, MemorySearchScope,
    MemoryWriteRequest,
};
pub use scheduler::{
    AddCronJobApiRequest, AddCronJobResult, ExecutionHistory, ExecutionStatus, NightWindowConfig,
    RemoveJobResult, ScheduledTask, SchedulerJobEnabledResult, SchedulerStatus,
    SchedulerTriggerResult,
};
pub use search::{AstQuery, SearchMode, SearchQuery, SearchResult, SearchResultItem, SymbolKind};
