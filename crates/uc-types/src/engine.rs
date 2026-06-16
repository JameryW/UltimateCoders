//! The EngineApi trait — the unified contract for all engine operations.
//!
//! Implemented twice:
//! - `LocalEngine` (uc-engine): direct calls to core components (in-process)
//! - `GrpcEngineClient` (uc-grpc): calls to remote gRPC server
//!
//! Python consumers switch between modes at construction time via the
//! `uc-python` binding layer.

use crate::error::EngineError;
use crate::index::{IndexRequest, IndexResponse};
use crate::memory::{
    MemoryEntry, MemoryReadRequest, MemorySearchRequest, MemorySearchResponse, MemoryWriteRequest,
};
use crate::search::{SearchQuery, SearchResult};
use async_trait::async_trait;

/// The core engine API — all operations the system supports.
///
/// Both local (PyO3) and remote (gRPC) modes implement this trait.
/// The Python binding exposes a single `Engine` class that delegates
/// to whichever implementation is active.
#[async_trait]
pub trait EngineApi: Send + Sync {
    // ── Search ──────────────────────────────────────────────

    /// Search across indexed repositories.
    async fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError>;

    // ── Indexing ────────────────────────────────────────────

    /// Index a repository (full or incremental).
    async fn index_repo(&self, request: IndexRequest) -> Result<IndexResponse, EngineError>;

    /// Get the current index state for a repository.
    async fn get_index_state(&self, repo_id: &str) -> Result<RepoIndexState, EngineError>;

    /// Remove a repository's index.
    async fn remove_index(&self, repo_id: &str) -> Result<(), EngineError>;

    // ── Memory ──────────────────────────────────────────────

    /// Read a memory entry.
    async fn read_memory(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError>;

    /// Write a memory entry.
    async fn write_memory(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError>;

    /// Delete a memory entry.
    async fn delete_memory(&self, key: &crate::memory::MemoryKey) -> Result<(), EngineError>;

    /// Search long-term memory semantically.
    async fn search_memory(
        &self,
        request: MemorySearchRequest,
    ) -> Result<MemorySearchResponse, EngineError>;

    // ── Health ──────────────────────────────────────────────

    /// Check engine health.
    async fn health(&self) -> Result<HealthStatus, EngineError>;

    // ── Future Extension Points ────────────────────────────────
    // TODO(future): Batch memory write for high-throughput agent coordination
    // async fn batch_write_memory(&self, requests: Vec<MemoryWriteRequest>) -> Result<Vec<MemoryEntry>, EngineError>;

    // TODO(future): List all indexed repositories
    // async fn list_repos(&self) -> Result<Vec<RepoIndexState>, EngineError>;

    // TODO(future): Stream search results for large result sets
    // async fn search_stream(&self, query: SearchQuery) -> Result<tokio_stream::BoxStream<'static, SearchResult>, EngineError>;
}

/// Index state returned by get_index_state.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RepoIndexState {
    pub repo_id: String,
    pub indexed: bool,
    pub last_indexed_sha: Option<String>,
    pub files_count: u32,
    pub symbols_count: u32,
    pub chunks_count: u32,
}

/// Engine health status.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct HealthStatus {
    pub status: String,
    pub version: String,
    pub uptime_seconds: u64,
    pub components: Vec<ComponentHealth>,
}

/// Health of an individual component.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct ComponentHealth {
    pub name: String,
    pub status: String,
    pub details: Option<String>,
}
