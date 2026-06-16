//! Long-term memory store backed by Qdrant.
//!
//! Stores project-scoped knowledge (architecture embeddings, decision history,
//! pattern accumulation) as vector points for semantic retrieval.
//! Collection: "memory_embeddings" with payload indexes on scope, source_agent, tags.

use uc_types::error::EngineError;
#[allow(unused_imports)]
use uc_types::memory::{
    MemoryContent, MemoryEntry, MemoryId, MemoryKey, MemoryMetadata, MemorySearchResult,
    MemorySearchScope,
};

#[cfg(feature = "storage")]
use qdrant_client::qdrant::{
    Condition, CreateCollectionBuilder, DeletePointsBuilder, Distance, Filter, PointStruct,
    SearchPointsBuilder, VectorParamsBuilder,
};
#[cfg(feature = "storage")]
use std::collections::HashMap;
#[cfg(feature = "storage")]
use std::sync::Arc;

use std::sync::Arc as StdArc;
use tokio::sync::RwLock;

/// The Qdrant collection name for memory embeddings.
#[cfg(feature = "storage")]
const COLLECTION_NAME: &str = "memory_embeddings";

/// Vector dimension for embeddings (Voyage Code 3 = 1024).
pub const VECTOR_SIZE: usize = 1024;

/// In-memory fallback entry for when Qdrant is unavailable.
#[derive(Debug, Clone)]
struct FallbackEntry {
    id: String,
    vector: Vec<f32>,
    entry: MemoryEntry,
}

/// Long-term memory store backed by Qdrant (vector search).
///
/// Supports semantic search over memory entries using vector similarity.
/// Each entry is stored as a point with its embedding vector and metadata payload.
pub struct LongTermMemory {
    #[cfg(feature = "storage")]
    client: Option<Arc<qdrant_client::Qdrant>>,
    /// In-memory fallback when Qdrant is not available (for testing / local dev).
    fallback: StdArc<RwLock<Vec<FallbackEntry>>>,
}

impl LongTermMemory {
    /// Create a new long-term memory store, connecting to Qdrant.
    #[cfg(feature = "storage")]
    pub async fn new(url: &str, api_key: Option<&str>) -> Result<Self, EngineError> {
        let mut builder = qdrant_client::Qdrant::from_url(url);
        if let Some(key) = api_key {
            builder = builder.api_key(key);
        }

        match builder.build() {
            Ok(client) => {
                let client = Arc::new(client);
                let store = Self {
                    client: Some(client.clone()),
                    fallback: StdArc::new(RwLock::new(Vec::new())),
                };

                // Ensure collection exists
                store.ensure_collection().await?;

                tracing::info!("Connected to Qdrant for long-term memory");
                Ok(store)
            }
            Err(e) => {
                tracing::warn!(
                    "Qdrant unavailable, using in-memory fallback for long-term memory: {}",
                    e
                );
                Ok(Self {
                    client: None,
                    fallback: StdArc::new(RwLock::new(Vec::new())),
                })
            }
        }
    }

    /// Create a new long-term memory store (fallback-only when storage feature is disabled).
    #[cfg(not(feature = "storage"))]
    pub async fn new(_url: &str, _api_key: Option<&str>) -> Result<Self, EngineError> {
        tracing::info!("Storage feature disabled, using in-memory fallback for long-term memory");
        Ok(Self::new_fallback())
    }

    /// Create with an existing Qdrant client (for testing / dependency injection).
    #[cfg(feature = "storage")]
    pub fn with_client(client: Arc<qdrant_client::Qdrant>) -> Self {
        Self {
            client: Some(client),
            fallback: StdArc::new(RwLock::new(Vec::new())),
        }
    }

    /// Create with in-memory fallback only (for testing).
    pub fn new_fallback() -> Self {
        Self {
            #[cfg(feature = "storage")]
            client: None,
            fallback: StdArc::new(RwLock::new(Vec::new())),
        }
    }

    /// Ensure the memory_embeddings collection exists in Qdrant.
    #[cfg(feature = "storage")]
    async fn ensure_collection(&self) -> Result<(), EngineError> {
        let client = self
            .client
            .as_ref()
            .ok_or_else(|| EngineError::ConnectionError("Qdrant client not available".into()))?;

        let collections = client.list_collections().await.map_err(|e| {
            EngineError::ConnectionError(format!("Qdrant list collections error: {}", e))
        })?;

        let exists = collections
            .collections
            .iter()
            .any(|c| c.name == COLLECTION_NAME);

        if !exists {
            client
                .create_collection(
                    CreateCollectionBuilder::new(COLLECTION_NAME).vectors_config(
                        VectorParamsBuilder::new(VECTOR_SIZE as u64, Distance::Cosine),
                    ),
                )
                .await
                .map_err(|e| {
                    EngineError::ConnectionError(format!("Qdrant create collection error: {}", e))
                })?;

            tracing::info!("Created Qdrant collection: {}", COLLECTION_NAME);
        }

        Ok(())
    }

    /// Write a memory entry with its embedding vector to Qdrant.
    ///
    /// The entry must have an embedding vector in its metadata.
    /// If no embedding is provided, the write succeeds but the point will
    /// have a zero vector (not searchable by semantic search).
    pub async fn write(&self, entry: &MemoryEntry) -> Result<(), EngineError> {
        let vector = entry
            .metadata
            .embedding
            .clone()
            .unwrap_or_else(|| vec![0.0f32; VECTOR_SIZE]);

        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            let qdrant_payload = entry_to_payload(entry);
            let point_id = entry_id_to_point_id(&entry.id);

            let point = PointStruct::new(
                point_id,
                vector.clone(),
                qdrant_client::Payload::from(qdrant_payload),
            );

            client
                .upsert_points(
                    qdrant_client::qdrant::UpsertPointsBuilder::new(COLLECTION_NAME, vec![point])
                        .wait(true),
                )
                .await
                .map_err(|e| {
                    EngineError::MemoryWriteError(format!("Qdrant upsert error: {}", e))
                })?;
        } else {
            let mut fallback = self.fallback.write().await;
            // Remove existing entry with same ID
            fallback.retain(|f| f.id != entry.id.0);
            fallback.push(FallbackEntry {
                id: entry.id.0.clone(),
                vector,
                entry: entry.clone(),
            });
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            // Remove existing entry with same ID
            fallback.retain(|f| f.id != entry.id.0);
            fallback.push(FallbackEntry {
                id: entry.id.0.clone(),
                vector,
                entry: entry.clone(),
            });
        }

        Ok(())
    }

    /// Delete a memory entry by its MemoryKey.
    pub async fn delete(&self, key: &MemoryKey) -> Result<(), EngineError> {
        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            // Use a filter to match the key in the payload.
            let filter = key_to_filter(key);

            client
                .delete_points(
                    DeletePointsBuilder::new(COLLECTION_NAME)
                        .points(filter)
                        .wait(true),
                )
                .await
                .map_err(|e| {
                    EngineError::MemoryWriteError(format!("Qdrant delete error: {}", e))
                })?;
        } else {
            let mut fallback = self.fallback.write().await;
            let key_str = encode_key_for_payload(key);
            fallback.retain(|f| encode_key_for_payload(&f.entry.key) != key_str);
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            let key_str = encode_key_for_payload(key);
            fallback.retain(|f| encode_key_for_payload(&f.entry.key) != key_str);
        }

        Ok(())
    }

    /// Search long-term memory by embedding vector similarity.
    ///
    /// Returns entries ranked by cosine similarity to the query embedding,
    /// filtered by the given scope.
    pub async fn search(
        &self,
        query_embedding: Vec<f32>,
        scope: &MemorySearchScope,
        max_results: u32,
        min_score: f32,
    ) -> Result<Vec<MemorySearchResult>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            let mut search_builder =
                SearchPointsBuilder::new(COLLECTION_NAME, query_embedding, max_results as u64)
                    .score_threshold(min_score)
                    .with_payload(true);

            if let Some(filter) = scope_to_filter(scope) {
                search_builder = search_builder.filter(filter);
            }

            let response = client
                .search_points(search_builder)
                .await
                .map_err(|e| EngineError::MemoryReadError(format!("Qdrant search error: {}", e)))?;

            let results = response
                .result
                .into_iter()
                .filter_map(|scored_point| {
                    let payload = scored_point.payload;
                    let entry = payload_to_entry(&payload)?;
                    Some(MemorySearchResult {
                        entry,
                        score: scored_point.score,
                    })
                })
                .collect();

            Ok(results)
        } else {
            // Fallback: naive linear scan with cosine similarity
            let fallback = self.fallback.read().await;
            let query_norm = vector_norm(&query_embedding);

            let mut results: Vec<MemorySearchResult> = fallback
                .iter()
                .filter(|f| matches_scope(&f.entry.key, scope))
                .filter_map(|f| {
                    let score = cosine_similarity(&query_embedding, &f.vector, query_norm);
                    if score >= min_score {
                        Some(MemorySearchResult {
                            entry: f.entry.clone(),
                            score,
                        })
                    } else {
                        None
                    }
                })
                .collect();

            results.sort_by(|a, b| {
                b.score
                    .partial_cmp(&a.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            results.truncate(max_results as usize);
            Ok(results)
        }

        #[cfg(not(feature = "storage"))]
        {
            // Fallback: naive linear scan with cosine similarity
            let fallback = self.fallback.read().await;
            let query_norm = vector_norm(&query_embedding);

            let mut results: Vec<MemorySearchResult> = fallback
                .iter()
                .filter(|f| matches_scope(&f.entry.key, scope))
                .filter_map(|f| {
                    let score = cosine_similarity(&query_embedding, &f.vector, query_norm);
                    if score >= min_score {
                        Some(MemorySearchResult {
                            entry: f.entry.clone(),
                            score,
                        })
                    } else {
                        None
                    }
                })
                .collect();

            results.sort_by(|a, b| {
                b.score
                    .partial_cmp(&a.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            results.truncate(max_results as usize);
            Ok(results)
        }
    }

    /// Check if the external storage backend (Qdrant) is connected.
    ///
    /// Returns `false` for in-memory fallback mode — the store is
    /// **functional** but not connected to a persistent backend.
    pub fn is_connected(&self) -> bool {
        #[cfg(feature = "storage")]
        {
            self.client.is_some()
        }
        #[cfg(not(feature = "storage"))]
        {
            false
        }
    }
}

// ── Qdrant-specific helpers (only compiled with storage feature) ──────

/// Convert a MemoryEntry to a Qdrant Payload (HashMap<String, qdrant_client::qdrant::Value>).
#[cfg(feature = "storage")]
fn entry_to_payload(entry: &MemoryEntry) -> HashMap<String, qdrant_client::qdrant::Value> {
    let mut payload: HashMap<String, qdrant_client::qdrant::Value> = HashMap::new();

    // Key as string
    payload.insert(
        "key".into(),
        qdrant_client::qdrant::Value::from(encode_key_for_payload(&entry.key)),
    );

    // Scope as a separate field for efficient filtering.
    // Unlike the "key" field which includes the full path (e.g., "project:proj1:architecture"),
    // the "scope" field contains just the scope prefix (e.g., "project:proj1") so we can
    // use exact-match filtering instead of prefix matching (which Qdrant doesn't natively support).
    let scope_str = scope_of_key(&entry.key);
    payload.insert(
        "scope".into(),
        qdrant_client::qdrant::Value::from(scope_str),
    );

    // Content as JSON string
    let content_json = serde_json::to_string(&entry.content).unwrap_or_default();
    payload.insert(
        "content".into(),
        qdrant_client::qdrant::Value::from(content_json),
    );

    // Source agent
    payload.insert(
        "source_agent".into(),
        qdrant_client::qdrant::Value::from(entry.metadata.source_agent.clone()),
    );

    // Importance
    payload.insert(
        "importance".into(),
        qdrant_client::qdrant::Value::from(entry.metadata.importance as f64),
    );

    // Tags
    let tags: Vec<qdrant_client::qdrant::Value> = entry
        .metadata
        .tags
        .iter()
        .cloned()
        .map(qdrant_client::qdrant::Value::from)
        .collect();
    payload.insert("tags".into(), qdrant_client::qdrant::Value::from(tags));

    // Timestamps
    payload.insert(
        "created_at".into(),
        qdrant_client::qdrant::Value::from(entry.created_at.to_rfc3339()),
    );
    payload.insert(
        "updated_at".into(),
        qdrant_client::qdrant::Value::from(entry.updated_at.to_rfc3339()),
    );

    // Store the entry ID for reconstruction
    payload.insert(
        "entry_id".into(),
        qdrant_client::qdrant::Value::from(entry.id.0.clone()),
    );

    payload
}

/// Convert a Qdrant payload (HashMap) back to a MemoryEntry.
#[cfg(feature = "storage")]
fn payload_to_entry(
    payload: &HashMap<String, qdrant_client::qdrant::Value>,
) -> Option<MemoryEntry> {
    let key_str = payload.get("key")?.as_string()?;
    let key = decode_key_from_payload(&key_str)?;

    let content_str = payload.get("content")?.as_string()?;
    let content: MemoryContent = serde_json::from_str(&content_str).ok()?;

    let source_agent = payload.get("source_agent")?.as_string()?;
    let importance = payload
        .get("importance")
        .and_then(|v| v.as_double())
        .map(|v| v as f32)
        .unwrap_or(0.0);

    let tags: Vec<String> = payload
        .get("tags")
        .and_then(|v| v.as_list())
        .map(|list| list.iter().filter_map(|v| v.as_string()).collect())
        .unwrap_or_default();

    let created_at: chrono::DateTime<chrono::Utc> = payload
        .get("created_at")
        .and_then(|v| v.as_string())
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(chrono::Utc::now);

    let updated_at: chrono::DateTime<chrono::Utc> = payload
        .get("updated_at")
        .and_then(|v| v.as_string())
        .and_then(|s| s.parse().ok())
        .unwrap_or_else(chrono::Utc::now);

    let id = payload
        .get("entry_id")
        .and_then(|v| v.as_string())
        .map(|s| MemoryId(s.to_string()))
        .unwrap_or_default();

    Some(MemoryEntry {
        id,
        key,
        content,
        metadata: MemoryMetadata {
            source_agent,
            importance,
            tags,
            embedding: None, // Embedding is stored in the vector, not payload
        },
        created_at,
        updated_at,
    })
}

/// Convert a MemoryKey to a Qdrant filter condition for payload matching.
#[cfg(feature = "storage")]
fn key_to_filter(key: &MemoryKey) -> Filter {
    let key_str = encode_key_for_payload(key);
    Filter::must([Condition::matches("key", key_str)])
}

/// Derive the scope prefix from a MemoryKey for use as a filterable payload field.
///
/// This produces a short, exact-matchable string like "project:proj1" or "global"
/// that can be stored in the Qdrant payload and matched efficiently with
/// `Condition::matches("scope", ...)`.
#[cfg(feature = "storage")]
fn scope_of_key(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { task_id, .. } => format!("task:{}", task_id),
        MemoryKey::Project { project_id, .. } => format!("project:{}", project_id),
        MemoryKey::Global { .. } => "global".to_string(),
    }
}

/// Convert a MemorySearchScope to a Qdrant filter.
///
/// Uses the "scope" payload field for exact-match filtering. This avoids the need
/// for prefix matching on the "key" field, which Qdrant does not natively support.
#[cfg(feature = "storage")]
fn scope_to_filter(scope: &MemorySearchScope) -> Option<Filter> {
    match scope {
        MemorySearchScope::Project { project_id } => Some(Filter::must([Condition::matches(
            "scope",
            format!("project:{}", project_id),
        )])),
        MemorySearchScope::Global => Some(Filter::must([Condition::matches(
            "scope",
            "global".to_string(),
        )])),
        MemorySearchScope::All => None,
    }
}

/// Convert a MemoryId to a Qdrant PointId.
#[cfg(feature = "storage")]
fn entry_id_to_point_id(id: &MemoryId) -> qdrant_client::qdrant::PointId {
    // Use the hash of the ID string as a numeric point ID.
    // This ensures deterministic mapping.
    let hash = blake3::hash(id.0.as_bytes());
    let numeric_id = u64::from_be_bytes(hash.as_bytes()[..8].try_into().unwrap_or([0u8; 8]));
    qdrant_client::qdrant::PointId::from(numeric_id)
}

/// Helper trait to extract string values from qdrant Value.
#[cfg(feature = "storage")]
trait ValueExt {
    fn as_string(&self) -> Option<String>;
    #[allow(dead_code)]
    fn as_double(&self) -> Option<f64>;
    #[allow(dead_code)]
    fn as_list(&self) -> Option<Vec<&qdrant_client::qdrant::Value>>;
}

#[cfg(feature = "storage")]
impl ValueExt for qdrant_client::qdrant::Value {
    fn as_string(&self) -> Option<String> {
        match &self.kind {
            Some(qdrant_client::qdrant::value::Kind::StringValue(s)) => Some(s.clone()),
            Some(qdrant_client::qdrant::value::Kind::NullValue(_)) => None,
            _ => None,
        }
    }

    fn as_double(&self) -> Option<f64> {
        match &self.kind {
            Some(qdrant_client::qdrant::value::Kind::DoubleValue(d)) => Some(*d),
            Some(qdrant_client::qdrant::value::Kind::IntegerValue(i)) => Some(*i as f64),
            _ => None,
        }
    }

    fn as_list(&self) -> Option<Vec<&qdrant_client::qdrant::Value>> {
        match &self.kind {
            Some(qdrant_client::qdrant::value::Kind::ListValue(list)) => {
                Some(list.values.iter().collect())
            }
            _ => None,
        }
    }
}

// ── Shared helpers (no Qdrant dependency) ─────────────────────────────

/// Encode a MemoryKey to a string for payload storage.
fn encode_key_for_payload(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task {
            task_id,
            key: inner,
        } => format!("task:{}:{}", task_id, inner),
        MemoryKey::Project {
            project_id,
            key: inner,
        } => format!("project:{}:{}", project_id, inner),
        MemoryKey::Global { key: inner } => format!("global:{}", inner),
    }
}

/// Decode a key string from payload back to a MemoryKey.
#[allow(dead_code)]
fn decode_key_from_payload(encoded: &str) -> Option<MemoryKey> {
    let parts: Vec<&str> = encoded.splitn(3, ':').collect();
    match *parts.first()? {
        "task" if parts.len() == 3 => Some(MemoryKey::Task {
            task_id: parts[1].to_string(),
            key: parts[2].to_string(),
        }),
        "project" if parts.len() == 3 => Some(MemoryKey::Project {
            project_id: parts[1].to_string(),
            key: parts[2].to_string(),
        }),
        "global" if parts.len() == 2 => Some(MemoryKey::Global {
            key: parts[1].to_string(),
        }),
        _ => None,
    }
}

/// Check if a MemoryKey matches the given search scope.
fn matches_scope(key: &MemoryKey, scope: &MemorySearchScope) -> bool {
    match scope {
        MemorySearchScope::Project { project_id } => {
            matches!(key, MemoryKey::Project { project_id: pid, .. } if pid == project_id)
        }
        MemorySearchScope::Global => matches!(key, MemoryKey::Global { .. }),
        MemorySearchScope::All => true,
    }
}

/// Compute the L2 norm of a vector.
fn vector_norm(v: &[f32]) -> f32 {
    v.iter().map(|x| x * x).sum::<f32>().sqrt()
}

/// Compute cosine similarity between two vectors.
fn cosine_similarity(a: &[f32], b: &[f32], a_norm: f32) -> f32 {
    if a.len() != b.len() || a_norm == 0.0 {
        return 0.0;
    }
    let b_norm = vector_norm(b);
    if b_norm == 0.0 {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    dot / (a_norm * b_norm)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_entry(key: MemoryKey, importance: f32, embedding: Option<Vec<f32>>) -> MemoryEntry {
        MemoryEntry {
            id: MemoryId::new(),
            key,
            content: MemoryContent::Text("test entry".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance,
                tags: vec!["test".to_string()],
                embedding,
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        }
    }

    #[test]
    fn test_encode_decode_key_payload() {
        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };
        let encoded = encode_key_for_payload(&task_key);
        assert_eq!(encoded, "task:t1:decisions");
        let decoded = decode_key_from_payload(&encoded).unwrap();
        assert_eq!(decoded, task_key);

        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };
        let encoded = encode_key_for_payload(&project_key);
        assert_eq!(encoded, "project:p1:architecture");
        let decoded = decode_key_from_payload(&encoded).unwrap();
        assert_eq!(decoded, project_key);

        let global_key = MemoryKey::Global {
            key: "conventions".to_string(),
        };
        let encoded = encode_key_for_payload(&global_key);
        assert_eq!(encoded, "global:conventions");
        let decoded = decode_key_from_payload(&encoded).unwrap();
        assert_eq!(decoded, global_key);
    }

    #[test]
    fn test_cosine_similarity() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![1.0, 0.0, 0.0];
        let norm = vector_norm(&a);
        let sim = cosine_similarity(&a, &b, norm);
        assert!((sim - 1.0).abs() < 0.001);

        let c = vec![0.0, 1.0, 0.0];
        let sim = cosine_similarity(&a, &c, norm);
        assert!((sim - 0.0).abs() < 0.001);
    }

    #[test]
    fn test_matches_scope() {
        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "k".to_string(),
        };
        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "k".to_string(),
        };
        let global_key = MemoryKey::Global {
            key: "k".to_string(),
        };

        assert!(matches_scope(
            &project_key,
            &MemorySearchScope::Project {
                project_id: "p1".to_string()
            }
        ));
        assert!(!matches_scope(
            &project_key,
            &MemorySearchScope::Project {
                project_id: "p2".to_string()
            }
        ));
        assert!(matches_scope(&global_key, &MemorySearchScope::Global));
        assert!(!matches_scope(&task_key, &MemorySearchScope::Global));
        assert!(matches_scope(&task_key, &MemorySearchScope::All));
    }

    #[tokio::test]
    async fn test_fallback_write_search_delete() {
        let store = LongTermMemory::new_fallback();

        // Write entries with small test vectors
        let entry1 = make_entry(
            MemoryKey::Project {
                project_id: "p1".to_string(),
                key: "arch".to_string(),
            },
            0.9,
            Some(vec![1.0, 0.0, 0.0, 0.0]),
        );
        let entry2 = make_entry(
            MemoryKey::Project {
                project_id: "p1".to_string(),
                key: "patterns".to_string(),
            },
            0.8,
            Some(vec![0.0, 1.0, 0.0, 0.0]),
        );

        store.write(&entry1).await.unwrap();
        store.write(&entry2).await.unwrap();

        // Search with a vector similar to entry1
        let query = vec![1.0, 0.0, 0.0, 0.0];
        let results = store
            .search(
                query,
                &MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
                10,
                0.0,
            )
            .await
            .unwrap();

        assert_eq!(results.len(), 2);
        // First result should be entry1 (cosine similarity = 1.0)
        assert!(results[0].score > results[1].score);

        // Delete entry1
        store.delete(&entry1.key).await.unwrap();
        let results = store
            .search(
                vec![1.0, 0.0, 0.0, 0.0],
                &MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
                10,
                0.0,
            )
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
    }

    #[tokio::test]
    async fn test_fallback_scope_filtering() {
        let store = LongTermMemory::new_fallback();

        let entry = make_entry(
            MemoryKey::Global {
                key: "conventions".to_string(),
            },
            0.5,
            Some(vec![1.0, 0.0]),
        );
        store.write(&entry).await.unwrap();

        // Should not match project scope
        let results = store
            .search(
                vec![1.0, 0.0],
                &MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
                10,
                0.0,
            )
            .await
            .unwrap();
        assert!(results.is_empty());

        // Should match global scope
        let results = store
            .search(vec![1.0, 0.0], &MemorySearchScope::Global, 10, 0.0)
            .await
            .unwrap();
        assert_eq!(results.len(), 1);
    }
}
