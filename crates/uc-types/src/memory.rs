//! Memory types for the layered memory system.
//!
//! Short-term: task-scoped context (TiKV-backed), volatile, fast KV access.
//! Long-term: project-scoped knowledge (Qdrant-backed), persistent, semantic search.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Unique identifier for a memory entry.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct MemoryId(pub String);

impl MemoryId {
    pub fn new() -> Self {
        Self(Uuid::new_v4().to_string())
    }
}

impl Default for MemoryId {
    fn default() -> Self {
        Self::new()
    }
}

/// Key for memory lookup — scoped to either a task or a project.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub enum MemoryKey {
    /// Short-term memory scoped to a task.
    Task { task_id: String, key: String },
    /// Long-term memory scoped to a project.
    Project { project_id: String, key: String },
    /// Global memory (cross-project patterns, conventions).
    Global { key: String },
}

/// A single memory entry stored in the system.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub id: MemoryId,
    pub key: MemoryKey,
    pub content: MemoryContent,
    pub metadata: MemoryMetadata,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: chrono::DateTime<chrono::Utc>,
    /// Monotonic version used for last-writer-wins reconciliation during
    /// gRPC-fallback replay. Defaults to the wall-clock millis of
    /// `created_at` so existing writes (which don't set it explicitly) get
    /// a sensible ordering. Upgradable to HLC without changing this field's
    /// "totally-ordered timestamp" semantics.
    #[serde(default = "default_version")]
    pub version: u64,
}

/// Default version: current wall-clock millis. Used when deserializing
/// entries written before the `version` field existed (backward compat).
fn default_version() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

impl MemoryEntry {
    /// Version derived from a timestamp (wall-clock millis).
    pub fn version_from_timestamp(ts: chrono::DateTime<chrono::Utc>) -> u64 {
        ts.timestamp_millis().max(0) as u64
    }
}

/// The content of a memory entry — different representations for different use cases.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum MemoryContent {
    /// Plain text (decisions, notes, observations).
    Text(String),
    /// Structured data (JSON).
    Structured(serde_json::Value),
    /// Code snippet with language tag.
    Code { language: String, code: String },
    /// Diff / patch.
    Diff { file_path: String, diff: String },
    /// Reference to an external resource.
    Reference { uri: String, description: String },
}

/// Metadata attached to a memory entry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryMetadata {
    /// Source agent that created this memory.
    pub source_agent: String,
    /// Importance score (0.0-1.0), used for eviction in short-term memory.
    pub importance: f32,
    /// Tags for categorization.
    pub tags: Vec<String>,
    /// Embedding vector (for long-term memory entries stored in Qdrant).
    pub embedding: Option<Vec<f32>>,
}

/// Read request for memory.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryReadRequest {
    pub key: MemoryKey,
    /// If true, also search long-term memory semantically.
    pub include_semantic: bool,
}

/// Write request for memory.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryWriteRequest {
    pub key: MemoryKey,
    pub content: MemoryContent,
    pub metadata: MemoryMetadata,
    /// Optional explicit version for replay (last-writer-wins). If `None`,
    /// the store assigns one from the current wall-clock time.
    #[serde(default)]
    pub version: Option<u64>,
}

/// Result of a replay write — the entry plus whether it was applied or
/// skipped as stale (last-writer-wins reconciliation).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryReplayResult {
    pub entry: MemoryEntry,
    pub applied: bool,
}

/// Search request within memory (semantic search over long-term memory).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemorySearchRequest {
    pub query: String,
    /// Scope filter.
    pub scope: MemorySearchScope,
    /// Maximum number of results.
    pub max_results: u32,
    /// Minimum similarity score (0.0-1.0).
    pub min_score: f32,
}

/// Scope for memory search.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum MemorySearchScope {
    Project { project_id: String },
    Global,
    All,
}

/// Response for memory search.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemorySearchResponse {
    pub results: Vec<MemorySearchResult>,
}

/// A single result from memory search.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemorySearchResult {
    pub entry: MemoryEntry,
    pub score: f32,
}
