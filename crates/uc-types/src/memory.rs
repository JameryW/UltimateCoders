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
