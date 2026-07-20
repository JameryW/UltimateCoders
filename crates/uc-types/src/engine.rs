//! The EngineApi trait — the unified contract for all engine operations.
//!
//! Implemented twice:
//! - `LocalEngine` (uc-engine): direct calls to core components (in-process)
//! - `GrpcEngineClient` (uc-grpc): calls to remote gRPC server
//!
//! Python consumers switch between modes at construction time via the
//! `uc-python` binding layer.

use crate::agent::{DirListing, FileContent, Task};
use crate::error::EngineError;
use crate::index::{IndexRequest, IndexResponse};
use crate::memory::{
    MemoryEntry, MemoryReadRequest, MemorySearchRequest, MemorySearchResponse, MemoryWriteRequest,
};
use crate::search::{SearchQuery, SearchResult};
use async_trait::async_trait;
use futures_core::Stream;
use std::pin::Pin;

/// Type alias for the stream returned by `search_stream`.
pub type SearchStream = Pin<Box<dyn Stream<Item = SearchResult> + Send>>;

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

    /// Incrementally re-index a single file from its content.
    ///
    /// Used by the `uc.file.changed` subscriber to keep the shared index fresh
    /// with worker edits without filesystem access to the worker's worktree.
    /// Default impl returns an error — only the gateway-side LocalEngine
    /// implements this; the gRPC client does not (workers don't reindex).
    async fn reindex_file(
        &self,
        repo_id: &str,
        file_path: &str,
        content: &str,
    ) -> Result<IndexResponse, EngineError> {
        let _ = (repo_id, file_path, content);
        Err(EngineError::IndexingError(
            "reindex_file not supported by this engine".into(),
        ))
    }

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

    /// Replay a memory write that happened during a gRPC fallback window.
    ///
    /// Last-writer-wins reconciliation by `MemoryWriteRequest.version`.
    /// Returns the entry and whether it was applied (`true`) or skipped
    /// as stale (`false`). Default impl delegates to `write_memory` for
    /// engines that don't need reconciliation.
    async fn replay_memory_write(
        &self,
        request: MemoryWriteRequest,
    ) -> Result<crate::memory::MemoryReplayResult, EngineError> {
        let entry = self.write_memory(request).await?;
        Ok(crate::memory::MemoryReplayResult {
            entry,
            applied: true,
        })
    }

    // ── Health ──────────────────────────────────────────────

    /// Check engine health.
    async fn health(&self) -> Result<HealthStatus, EngineError>;

    // ── Batch / List / Stream ────────────────────────────────

    /// Batch memory write for high-throughput agent coordination.
    ///
    /// Writes multiple memory entries in a single call. Returns the
    /// written entries in order, or the first error encountered.
    async fn batch_write_memory(
        &self,
        requests: Vec<MemoryWriteRequest>,
    ) -> Result<Vec<MemoryEntry>, EngineError>;

    /// List all indexed repositories.
    ///
    /// If `workspace_id` is `Some`, only repos in that workspace are returned.
    /// If `None`, all repos are returned (backward compatible).
    async fn list_repos(
        &self,
        workspace_id: Option<&str>,
    ) -> Result<Vec<RepoIndexState>, EngineError>;

    /// List directory contents in a repo (File Browser).
    async fn list_dir(&self, repo_id: &str, path: &str) -> Result<DirListing, EngineError>;

    /// Read file content from a repo (File Browser).
    async fn get_file(&self, repo_id: &str, path: &str) -> Result<FileContent, EngineError>;

    /// Stream search results for large result sets.
    ///
    /// The default implementation wraps a single `search()` call in
    /// a one-element stream. Implementors may override this to
    /// provide true streaming (e.g., server-side cursor).
    async fn search_stream(&self, query: SearchQuery) -> Result<SearchStream, EngineError>;

    // ── Task Orchestration ───────────────────────────────────

    /// Submit a new task for orchestration.
    async fn submit_task(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError>;

    /// Get a task by ID.
    async fn get_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// List all tasks.
    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError>;

    /// Pause a running task.
    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError>;

    /// Resume a paused task.
    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError>;
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
    /// Local filesystem path to the repo (for File Browser).
    pub local_path: Option<String>,
    /// Git remote URL (populated from repo metadata/config when listing repos).
    pub remote_url: Option<String>,
    /// Default branch name (populated from repo metadata/config when listing repos).
    pub default_branch: Option<String>,
    /// 工作目录 ID — 该 repo 所属的仓库集合。缺省 "default"。
    #[serde(default = "default_workspace_id")]
    pub workspace_id: String,
}

fn default_workspace_id() -> String {
    "default".to_string()
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
