//! # UltimateCoders - Shared Types
//!
//! Core type definitions shared across all crates.
//! No I/O, no framework dependencies — just data types and the EngineApi trait.

pub mod error;
pub mod memory;
pub mod search;
pub mod index;
pub mod agent;
pub mod engine;

// Re-exports for convenience
pub use async_trait::async_trait;
pub use error::EngineError;
pub use engine::{ComponentHealth, EngineApi, HealthStatus, RepoIndexState};
pub use index::{ChunkType, CodeChunk, IndexHealth, IndexRequest, IndexResponse, IndexState, RepoSpec};
pub use memory::{
    MemoryContent, MemoryEntry, MemoryId, MemoryKey, MemoryMetadata,
    MemoryReadRequest, MemorySearchRequest, MemorySearchResponse, MemorySearchResult,
    MemorySearchScope, MemoryWriteRequest,
};
pub use search::{
    AstQuery, SearchMode, SearchQuery, SearchResult, SearchResultItem, SymbolKind,
};
pub use agent::{
    AgentEvent, AgentEventPayload, ChangeType, FileChange, Subtask, SubtaskResult,
    SubtaskStatus, Task, TaskId, TaskStatus, WorkerId, WorkerInfo,
};
