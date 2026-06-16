//! Index types for the code indexing pipeline.

use serde::{Deserialize, Serialize};

/// Specification of a repository to be indexed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepoSpec {
    pub repo_id: String,
    /// Git remote URL.
    pub remote_url: String,
    /// Default branch name.
    pub default_branch: String,
    /// Local clone path (if already cloned).
    pub local_path: Option<String>,
}

/// Current state of a repository's index.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexState {
    pub repo_id: String,
    /// Last commit SHA that was fully indexed.
    pub last_indexed_sha: String,
    /// Timestamp of last successful index.
    pub last_indexed_at: chrono::DateTime<chrono::Utc>,
    /// Timestamp of last full reindex.
    pub last_full_reindex: chrono::DateTime<chrono::Utc>,
    /// Schema version of the index (for migration detection).
    pub index_version: u32,
    /// Index health status.
    pub health: IndexHealth,
    /// Number of files indexed.
    pub files_count: u64,
    /// Number of symbols extracted.
    pub symbols_count: u64,
    /// Number of code chunks embedded.
    pub chunks_count: u64,
}

/// Health status of a repository's index.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum IndexHealth {
    /// Index is up-to-date and consistent.
    Healthy,
    /// Index is being updated.
    Indexing,
    /// Index may be stale (webhook missed, etc.).
    Stale,
    /// Index is corrupted and needs full reindex.
    Corrupted,
}

/// A code chunk for embedding and indexing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodeChunk {
    /// Unique ID: BLAKE3(repo_id + file_path + start_line).
    pub id: String,
    pub repo_id: String,
    pub file_path: String,
    pub start_line: u32,
    pub end_line: u32,
    /// The actual code text.
    pub content: String,
    pub language: String,
    /// Name of the containing function/class (if chunk is a symbol).
    pub symbol_name: Option<String>,
    /// Kind of the symbol ("function", "class", "method", etc.).
    pub symbol_kind: Option<String>,
    /// Enclosing class for methods.
    pub parent_symbol: Option<String>,
    /// Granularity level.
    pub chunk_type: ChunkType,
    /// Content hash for incremental update detection.
    pub content_hash: String,
}

/// Granularity level of a code chunk.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum ChunkType {
    /// Full file content.
    File,
    /// A single symbol (function, class, etc.).
    Symbol,
    /// A block within a symbol (for oversized functions).
    Block,
}

/// Request to index a repository.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexRequest {
    pub repo: RepoSpec,
    /// Force full reindex even if incremental is possible.
    pub force_full: bool,
}

/// Response from an index operation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexResponse {
    pub repo_id: String,
    pub files_indexed: u32,
    pub symbols_extracted: u32,
    pub chunks_embedded: u32,
    pub duration_ms: u64,
}
