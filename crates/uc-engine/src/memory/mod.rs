//! Memory module — layered memory system (short-term TiKV + long-term Qdrant).
//!
//! Read path: check short-term first, then long-term if `include_semantic`.
//! Write path: always write to short-term; if importance > threshold, also write
//!             to long-term (with embedding if available).
//! Search path: semantic search in long-term via Qdrant.

pub mod long_term;
pub mod short_term;

use uc_types::error::EngineError;
use uc_types::memory::{
    MemoryContent, MemoryEntry, MemoryKey, MemoryReadRequest, MemorySearchRequest,
    MemorySearchResponse, MemorySearchResult, MemorySearchScope, MemoryWriteRequest,
};

use crate::config::MemoryConfig;
use crate::indexer::semantic::EmbeddingService;
use crate::memory::long_term::LongTermMemory;
use crate::memory::short_term::ShortTermMemory;

use std::sync::Arc;

use dashmap::DashMap;
use tokio::sync::Mutex;

/// Tag marking a short-term entry as a leave-behind tombstone.
///
/// A delete removes the short-term entry eagerly but the long-term (Qdrant)
/// vector delete is best-effort: a failed dual-write used to leave a zombie
/// vector that `read(include_semantic=true)` and `search()` could return
/// forever. The compensation tombstone records the deletion in the same
/// short-term store (and in the in-process map), so both recall paths ignore
/// the key until a later `write()` revives it.
const TOMBSTONE_TAG: &str = "__tombstone__";

/// Tag prefix for an entry-level TTL, seconds after `updated_at`.
const TTL_TAG_PREFIX: &str = "ttl:";

/// Tag prefix for an absolute expiry instant (RFC 3339).
const EXPIRES_AT_TAG_PREFIX: &str = "expires_at:";

/// Unified memory store that coordinates short-term and long-term storage.
///
/// Short-term memory (TiKV): fast KV access for task-scoped context.
/// Long-term memory (Qdrant): semantic search for project-scoped knowledge.
pub struct MemoryStore {
    short_term: Arc<ShortTermMemory>,
    long_term: Arc<LongTermMemory>,
    embedding_service: Arc<EmbeddingService>,
    config: MemoryConfig,
    /// Per-key locks for replay reconciliation (last-writer-wins by version).
    /// ponytail: in-process mutex is safe because there's a single gateway
    /// instance; upgrade to TiKV-native CAS (`with_atomic_for_cas`) if we
    /// ever go multi-gateway.
    replay_locks: DashMap<String, Arc<Mutex<()>>>,
    /// Last entry applied by `replay_write` for a key, keyed like the lock.
    ///
    /// The durable `MemoryEntry.version` is the epoch of the last committed
    /// state; this map is the process-local Lamport sequence of replays that
    /// may arrive out of order or twice. A replay must be strictly newer than
    /// both (see `replay_write`), and the stored entry lets a stale replay
    /// still return the state it did not overwrite.
    replay_seen: DashMap<String, MemoryEntry>,
    /// Keys deleted but not guaranteed removed from the long-term store,
    /// keyed by the encoded key with the deletion wall-clock millis as value.
    tombstones: DashMap<String, u64>,
}

impl MemoryStore {
    /// Create a new unified memory store.
    pub fn new(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
        embedding_service: Arc<EmbeddingService>,
        config: MemoryConfig,
    ) -> Self {
        Self {
            short_term,
            long_term,
            embedding_service,
            config,
            replay_locks: DashMap::new(),
            replay_seen: DashMap::new(),
            tombstones: DashMap::new(),
        }
    }

    /// Create with default config.
    pub fn new_with_default_config(
        short_term: Arc<ShortTermMemory>,
        long_term: Arc<LongTermMemory>,
        embedding_service: Arc<EmbeddingService>,
    ) -> Self {
        Self {
            short_term,
            long_term,
            embedding_service,
            config: MemoryConfig::default(),
            replay_locks: DashMap::new(),
            replay_seen: DashMap::new(),
            tombstones: DashMap::new(),
        }
    }

    /// Read a memory entry.
    ///
    /// Checks short-term memory first. If not found and `include_semantic` is true,
    /// falls back to long-term memory using semantic search with the key as query text.
    pub async fn read(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError> {
        // Always check short-term first. A leave-behind tombstone means the key
        // was deleted but the long-term vector may survive; treat it as absent
        // and do NOT fall through to the semantic path (that would resurrect
        // exactly the entry the delete was meant to hide).
        let result = self.short_term.read(&request.key).await?;
        if let Some(entry) = result {
            if is_tombstone(&entry) {
                return Ok(None);
            }
            return Ok(Some(entry));
        }

        // If not found and semantic lookup requested, search long-term memory
        if request.include_semantic {
            // Derive query text from the key's inner key field.
            //
            // NOTE (BLAKE3 limitation): Entries written via `write()` are embedded
            // from their *content* text, but `read()` searches using the *key* text.
            // With Voyage semantic embeddings, key "architecture" and content
            // "Use microservices for scaling" are semantically related, so the
            // search can still find a match. With the BLAKE3 fallback, however,
            // embeddings are deterministic per exact text — different strings
            // produce unrelated vectors. This means `read(include_semantic=true)`
            // may not find entries via BLAKE3 unless the key text happens to
            // overlap with the content text that was embedded at write time.
            let query_text = key_to_query_text(&request.key);
            let scope = key_to_search_scope(&request.key);

            match self.embedding_service.embed_single(&query_text).await {
                Ok(query_embedding) => {
                    let results = self
                        .long_term
                        .search(query_embedding, &scope, 3, self.config.min_search_score)
                        .await?;
                    // Validate every candidate before accepting it: a semantic
                    // hit from another scope, a deleted key, or an expired entry
                    // is a zombie recall, not memory. Taking only the first hit
                    // would also lose a valid second one behind a filtered first.
                    for candidate in results {
                        if !key_scope_matches(&request.key, &candidate.entry.key) {
                            tracing::debug!(
                                "semantic read skipped out-of-scope hit (requested={:?} hit={:?})",
                                request.key,
                                candidate.entry.key
                            );
                            continue;
                        }
                        if ttl_expired(&candidate.entry, chrono::Utc::now()) {
                            tracing::debug!(
                                "semantic read skipped expired hit (key={:?})",
                                candidate.entry.key
                            );
                            continue;
                        }
                        if self.is_deleted(&candidate.entry.key).await? {
                            tracing::debug!(
                                "semantic read skipped tombstoned hit (key={:?})",
                                candidate.entry.key
                            );
                            continue;
                        }
                        return Ok(Some(candidate.entry));
                    }
                }
                Err(e) => {
                    // Graceful degradation: log warning and return None
                    tracing::warn!(
                        "Embedding generation failed for semantic read, returning None: {}",
                        e
                    );
                }
            }
        }

        Ok(None)
    }

    /// Write a memory entry.
    ///
    /// Always writes to short-term memory. If the entry's importance exceeds
    /// the configured threshold, also writes to long-term memory (generating
    /// an embedding if one is not already provided).
    pub async fn write(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError> {
        let now = chrono::Utc::now();
        let encoded_key = crate::memory::short_term::encode_key(&request.key);
        let version = request
            .version
            .unwrap_or_else(|| MemoryEntry::version_from_timestamp(now));
        let mut entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: request.key,
            content: request.content,
            metadata: request.metadata,
            created_at: now,
            updated_at: now,
            version,
        };

        // A write revives a key: clear the compensation tombstone before the
        // short-term write so reads stop treating the key as deleted.
        self.tombstones.remove(&encoded_key);

        // Always write to short-term
        self.short_term.write(&entry).await?;

        // Write to long-term if importance exceeds threshold
        if entry.metadata.importance >= self.config.long_term_importance_threshold {
            // Generate embedding if not already provided, so semantic search can find it
            if entry.metadata.embedding.is_none() {
                let content_text = content_to_text(&entry.content);
                match self.embedding_service.embed_single(&content_text).await {
                    Ok(embedding) => {
                        entry.metadata.embedding = Some(embedding);
                    }
                    Err(e) => {
                        // Log but continue — LongTermMemory::write() falls back to
                        // a zero vector when embedding is None, so the entry is still
                        // stored but won't be findable via semantic search
                        tracing::warn!(
                            "Failed to generate embedding for long-term memory write: {}",
                            e
                        );
                    }
                }
            }
            match self.long_term.write(&entry).await {
                Ok(()) => {
                    tracing::debug!(
                        "Wrote entry to long-term memory (importance={:.2})",
                        entry.metadata.importance
                    );
                }
                Err(e) => {
                    // Log but don't fail — short-term write succeeded
                    tracing::warn!("Failed to write to long-term memory: {}", e);
                }
            }
        }

        Ok(entry)
    }

    /// Replay a memory write that happened during a gRPC fallback window.
    ///
    /// Last-writer-wins by a monotonic version pair: the durable entry version
    /// is the *epoch* (what was last committed to the store) and the process
    /// high-water mark is the *Lamport sequence* for replays that may arrive out
    /// of order or twice after a fallback window. A replay is applied only when
    /// it is strictly newer than both; equal versions are duplicate deliveries,
    /// not new state. The comparison happens inside the per-key lock, and the
    /// high-water mark also covers a short-term backend that still returns a
    /// stale read. This is the authoritative cross-worker reconciliation point
    /// for memory writes that bypassed the gateway while it was unreachable.
    pub async fn replay_write(
        &self,
        request: MemoryWriteRequest,
    ) -> Result<uc_types::memory::MemoryReplayResult, EngineError> {
        let encoded_key = crate::memory::short_term::encode_key(&request.key);
        // ponytail: per-key in-process lock — single gateway makes this safe.
        let lock = self
            .replay_locks
            .entry(encoded_key.clone())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone();
        let _guard = lock.lock().await;

        let stored = self.short_term.read(&request.key).await?;
        // A tombstone is a committed deletion, so it participates in the
        // race: an old replay must not resurrect a key deleted after it was
        // written. The tombstone's version is its deletion timestamp.
        let deleted_version = if stored.as_ref().map(is_tombstone).unwrap_or(false) {
            stored.as_ref().map(|entry| entry.version).unwrap_or(0)
        } else {
            0
        };
        let pending_version = request
            .version
            .unwrap_or_else(|| MemoryEntry::version_from_timestamp(chrono::Utc::now()));

        let last_seen = self
            .replay_seen
            .get(&encoded_key)
            .map(|entry| entry.clone());
        let durable = stored
            .as_ref()
            .filter(|entry| !is_tombstone(entry))
            .map(|entry| entry.version)
            .unwrap_or(0);
        let sequence = last_seen.as_ref().map(|entry| entry.version).unwrap_or(0);
        let floor = durable.max(sequence).max(deleted_version);

        if pending_version > 0 && pending_version <= floor {
            tracing::info!(
                "replay_write skipped stale or duplicate entry (key={:?} floor_v={} pending_v={})",
                request.key,
                floor,
                pending_version
            );
            // Return whatever newer state we refused to overwrite. A tombstone
            // is also a committed state, so it is the last resort when no
            // non-tombstone entry or replay high-water mark exists (for
            // example a key written through `write` and then deleted).
            let fallback = stored.clone();
            let entry = fallback
                .clone()
                .filter(|entry| !is_tombstone(entry))
                .or(last_seen)
                .or(fallback)
                .expect("floor>0 implies a stored tombstone, entry, or replay");
            return Ok(uc_types::memory::MemoryReplayResult {
                entry,
                applied: false,
            });
        }

        let req = MemoryWriteRequest {
            version: Some(pending_version),
            ..request
        };
        let entry = self.write(req).await?;
        self.replay_seen.insert(encoded_key, entry.clone());
        Ok(uc_types::memory::MemoryReplayResult {
            entry,
            applied: true,
        })
    }

    /// Delete a memory entry.
    ///
    /// Deletes from both short-term and long-term memory. The long-term
    /// (Qdrant) side is best-effort and can fail while the short-term side
    /// succeeds; a compensation tombstone is always left behind so semantic
    /// reads and searches cannot return the deleted entry again (a later
    /// `write()` clears it). This is the "mark-not-delete" half of the
    /// supersession story for the incremental layer — the audit trail lives in
    /// Hy-Memory, while this store must stop serving the stale vector.
    pub async fn delete(&self, key: &MemoryKey) -> Result<(), EngineError> {
        // Delete from short-term
        self.short_term.delete(key).await?;

        // Delete from long-term (best-effort)
        match self.long_term.delete(key).await {
            Ok(()) => {}
            Err(e) => {
                tracing::warn!("Failed to delete from long-term memory: {}", e);
            }
        }

        // Compensation tombstone: record the deletion even when both deletes
        // succeeded, because a concurrent reader may still hold the vector in
        // a cached search page. A later write() revives the key.
        self.write_tombstone(key).await?;

        Ok(())
    }

    /// Search long-term memory semantically.
    ///
    /// Internally uses the `EmbeddingService` to convert the query text into
    /// an embedding vector (BLAKE3 fallback or Voyage Code 3 API), then
    /// delegates to `search_with_embedding()`. If embedding generation fails,
    /// returns an empty result set with a warning log (graceful degradation).
    pub async fn search(
        &self,
        request: MemorySearchRequest,
    ) -> Result<MemorySearchResponse, EngineError> {
        let max_results = if request.max_results > 0 {
            request.max_results.min(self.config.max_search_results)
        } else {
            self.config.max_search_results
        };

        let min_score = if request.min_score > 0.0 {
            request.min_score
        } else {
            self.config.min_search_score
        };

        // Compute query embedding via EmbeddingService (BLAKE3 fallback or Voyage API)
        let query_embedding = match self.embedding_service.embed_single(&request.query).await {
            Ok(embedding) => embedding,
            Err(e) => {
                // Graceful degradation: return empty results instead of propagating error
                tracing::warn!(
                    "Embedding generation failed for memory search query '{}', returning empty results: {}",
                    request.query,
                    e
                );
                return Ok(MemorySearchResponse { results: vec![] });
            }
        };

        let results = self
            .long_term
            .search(query_embedding, &request.scope, max_results, min_score)
            .await?;

        Ok(MemorySearchResponse {
            results: self.retain_recallable(results, &request.scope).await?,
        })
    }

    /// Search with a pre-computed embedding vector.
    ///
    /// This is the preferred method when the caller has already computed
    /// the embedding for the query text.
    pub async fn search_with_embedding(
        &self,
        query_embedding: Vec<f32>,
        scope: MemorySearchScope,
        max_results: u32,
        min_score: f32,
    ) -> Result<Vec<MemorySearchResult>, EngineError> {
        let max_results = if max_results > 0 {
            max_results.min(self.config.max_search_results)
        } else {
            self.config.max_search_results
        };

        let min_score = if min_score > 0.0 {
            min_score
        } else {
            self.config.min_search_score
        };

        let results = self
            .long_term
            .search(query_embedding, &scope, max_results, min_score)
            .await?;
        self.retain_recallable(results, &scope).await
    }

    /// Drop semantic hits that must not be recalled: tombstones, expired
    /// entries, and (for an explicit project/global scope) entries from a
    /// different scope that a permissive backend returned. One batch pass, so
    /// the filter sees the same `now` for every hit and stays deterministic.
    async fn retain_recallable(
        &self,
        results: Vec<MemorySearchResult>,
        scope: &MemorySearchScope,
    ) -> Result<Vec<MemorySearchResult>, EngineError> {
        let now = chrono::Utc::now();
        let mut kept = Vec::with_capacity(results.len());
        for result in results {
            if !scope_allows(scope, &result.entry.key) {
                tracing::debug!(
                    "memory search skipped out-of-scope hit (scope={:?} hit={:?})",
                    scope,
                    result.entry.key
                );
                continue;
            }
            if ttl_expired(&result.entry, now) {
                tracing::debug!(
                    "memory search skipped expired hit (key={:?})",
                    result.entry.key
                );
                continue;
            }
            if self.is_deleted(&result.entry.key).await? {
                tracing::debug!(
                    "memory search skipped tombstoned hit (key={:?})",
                    result.entry.key
                );
                continue;
            }
            kept.push(result);
        }
        Ok(kept)
    }

    /// Whether a key is covered by a compensation tombstone.
    ///
    /// Checks the in-process map first (fast path, covers a failed long-term
    /// delete), then the short-term store (persisted marker, covers a gateway
    /// restart while `task_ttl_seconds` is still alive).
    async fn is_deleted(&self, key: &MemoryKey) -> Result<bool, EngineError> {
        let encoded = crate::memory::short_term::encode_key(key);
        if self.tombstones.contains_key(&encoded) {
            return Ok(true);
        }
        Ok(match self.short_term.read(key).await? {
            Some(entry) => is_tombstone(&entry),
            None => false,
        })
    }

    /// Leave a tombstone entry and record it in the in-process map.
    async fn write_tombstone(&self, key: &MemoryKey) -> Result<(), EngineError> {
        let now = chrono::Utc::now();
        let encoded = crate::memory::short_term::encode_key(key);
        self.tombstones
            .insert(encoded, now.timestamp_millis().max(0) as u64);
        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: key.clone(),
            content: MemoryContent::Text(String::new()),
            metadata: uc_types::memory::MemoryMetadata {
                source_agent: "uc-engine".to_string(),
                importance: 0.0,
                tags: vec![TOMBSTONE_TAG.to_string()],
                embedding: None,
            },
            created_at: now,
            updated_at: now,
            version: MemoryEntry::version_from_timestamp(now),
        };
        self.short_term.write(&entry).await?;
        Ok(())
    }

    /// List all keys in a scope (prefix scan on short-term memory).
    pub async fn list_keys(&self, key: &MemoryKey) -> Result<Vec<MemoryKey>, EngineError> {
        let prefix = crate::memory::short_term::scope_prefix(key);
        self.short_term.list_keys(&prefix).await
    }

    /// Get a reference to the long-term memory store.
    pub fn long_term(&self) -> &Arc<LongTermMemory> {
        &self.long_term
    }

    /// Get a reference to the embedding service.
    pub fn embedding_service(&self) -> &Arc<EmbeddingService> {
        &self.embedding_service
    }

    /// Health check for memory components.
    pub fn health(&self) -> Vec<uc_types::engine::ComponentHealth> {
        // An explicitly configured in-memory backend is not a degraded state:
        // the operator opted in (e.g. `UC_SHORT_TERM_BACKEND=memory` on a
        // platform without TiKV). Only an unexpected fallback is reported as
        // `fallback` (mapped to "degraded" by LocalEngine::health).
        let short_term_status =
            if self.short_term.is_connected() || self.short_term.is_intentional_memory() {
                "ok"
            } else {
                "fallback"
            };
        let short_term_details = if self.short_term.is_connected() {
            Some("TiKV connected".into())
        } else if self.short_term.is_intentional_memory() {
            Some("In-memory (explicitly configured via UC_SHORT_TERM_BACKEND=memory)".into())
        } else {
            Some("Using in-memory fallback".into())
        };
        vec![
            uc_types::engine::ComponentHealth {
                name: "short_term_memory".into(),
                status: short_term_status.into(),
                details: short_term_details,
            },
            uc_types::engine::ComponentHealth {
                name: "long_term_memory".into(),
                status: if self.long_term.is_connected() {
                    "ok".into()
                } else {
                    "fallback".into()
                },
                details: if self.long_term.is_connected() {
                    Some("Qdrant connected".into())
                } else {
                    Some("Using in-memory fallback".into())
                },
            },
        ]
    }
}

/// Whether an entry is a leave-behind deletion marker (see [`TOMBSTONE_TAG`]).
fn is_tombstone(entry: &MemoryEntry) -> bool {
    entry.metadata.tags.iter().any(|tag| tag == TOMBSTONE_TAG)
}

/// Whether an entry's own metadata declares it expired at `now`.
///
/// Long-term entries carry no TTL column, so expiry is expressed as a tag on
/// the entry: `ttl:<seconds>` (relative to `updated_at`) or
/// `expires_at:<rfc3339>`. Unparsable or absent tags never expire an entry —
/// a malformed hint must not delete knowledge silently.
fn ttl_expired(entry: &MemoryEntry, now: chrono::DateTime<chrono::Utc>) -> bool {
    for tag in &entry.metadata.tags {
        if let Some(raw) = tag.strip_prefix(TTL_TAG_PREFIX) {
            if let Ok(seconds) = raw.trim().parse::<i64>() {
                if seconds <= 0 {
                    return true;
                }
                if entry.updated_at + chrono::Duration::seconds(seconds) <= now {
                    return true;
                }
            }
        } else if let Some(raw) = tag.strip_prefix(EXPIRES_AT_TAG_PREFIX) {
            if let Ok(expires) = chrono::DateTime::parse_from_rfc3339(raw.trim()) {
                if expires <= now {
                    return true;
                }
            }
        }
    }
    false
}

/// Whether a semantic hit from `candidate` may satisfy a read for `requested`.
///
/// The store used to accept any long-term hit for a key (a task-scoped read
/// could return another task's or a global entry), which is exactly how a
/// deleted or unrelated memory came back as a "recall". Scopes must match by
/// variant and, for project/task keys, by id.
fn key_scope_matches(requested: &MemoryKey, candidate: &MemoryKey) -> bool {
    match (requested, candidate) {
        (MemoryKey::Global { .. }, MemoryKey::Global { .. }) => true,
        (
            MemoryKey::Project {
                project_id: requested_id,
                ..
            },
            MemoryKey::Project {
                project_id: candidate_id,
                ..
            },
        ) => requested_id == candidate_id,
        (
            MemoryKey::Task {
                task_id: requested_id,
                ..
            },
            MemoryKey::Task {
                task_id: candidate_id,
                ..
            },
        ) => requested_id == candidate_id,
        _ => false,
    }
}

/// Whether a result satisfies an explicit search scope.
///
/// `All` intentionally accepts every variant (it is the cross-scope search);
/// `Project` and `Global` are explicit filters and an entry from another
/// scope is a backend leak, not a match.
fn scope_allows(scope: &MemorySearchScope, key: &MemoryKey) -> bool {
    match scope {
        MemorySearchScope::Project { project_id } => matches!(
            key,
            MemoryKey::Project {
                project_id: candidate_id,
                ..
            } if candidate_id == project_id
        ),
        MemorySearchScope::Global => matches!(key, MemoryKey::Global { .. }),
        MemorySearchScope::All => true,
    }
}

/// Extract searchable text from `MemoryContent` for embedding generation.
fn content_to_text(content: &MemoryContent) -> String {
    match content {
        MemoryContent::Text(t) => t.clone(),
        MemoryContent::Structured(v) => v.to_string(),
        MemoryContent::Code { code, .. } => code.clone(),
        MemoryContent::Diff { diff, .. } => diff.clone(),
        MemoryContent::Reference { description, .. } => description.clone(),
    }
}

/// Extract query text from a `MemoryKey` for semantic search.
///
/// Uses the inner key field as the query text since it typically describes
/// what the memory entry is about (e.g., "architecture", "decisions").
fn key_to_query_text(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { key: inner, .. } => inner.clone(),
        MemoryKey::Project { key: inner, .. } => inner.clone(),
        MemoryKey::Global { key: inner } => inner.clone(),
    }
}

/// Derive a `MemorySearchScope` from a `MemoryKey` for semantic lookup.
///
/// Project-scoped keys search within that project, global keys search globally,
/// and task-scoped keys search within the associated project (using All scope
/// since tasks are not directly represented in long-term memory scopes).
fn key_to_search_scope(key: &MemoryKey) -> MemorySearchScope {
    match key {
        MemoryKey::Project { project_id, .. } => MemorySearchScope::Project {
            project_id: project_id.clone(),
        },
        MemoryKey::Global { .. } => MemorySearchScope::Global,
        MemoryKey::Task { .. } => MemorySearchScope::All,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::memory::{MemoryContent, MemoryMetadata};

    fn make_write_request(key: MemoryKey, importance: f32) -> MemoryWriteRequest {
        MemoryWriteRequest {
            key,
            content: MemoryContent::Text("test data".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            version: None,
        }
    }

    fn make_replay_request(key: MemoryKey, content: &str, version: u64) -> MemoryWriteRequest {
        MemoryWriteRequest {
            key,
            content: MemoryContent::Text(content.to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            version: Some(version),
        }
    }

    /// AC1: a replay write with a newer version is applied and readable.
    #[tokio::test]
    async fn test_replay_write_applies_newer_version() {
        let store = make_store().await;
        let key = MemoryKey::Global {
            key: "shared".to_string(),
        };

        // Worker B writes v100 first (the "current" gateway value).
        store
            .replay_write(make_replay_request(key.clone(), "B-wins", 100))
            .await
            .unwrap();

        // Worker A replays its fallback write at v50 (older) — must be skipped.
        let result = store
            .replay_write(make_replay_request(key.clone(), "A-stale", 50))
            .await
            .unwrap();
        assert!(!result.applied, "stale replay must be skipped");

        // Worker A replays a newer write at v200 — must be applied.
        let result = store
            .replay_write(make_replay_request(key.clone(), "A-fresh", 200))
            .await
            .unwrap();
        assert!(result.applied, "fresh replay must be applied");

        // The readable value is the freshest write.
        let read = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap()
            .expect("entry should exist");
        match read.content {
            MemoryContent::Text(t) => assert_eq!(t, "A-fresh"),
            _ => panic!("expected text content"),
        }
        assert_eq!(read.version, 200);
    }

    async fn make_store() -> MemoryStore {
        let short_term = Arc::new(ShortTermMemory::new_fallback(3600));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        MemoryStore::new_with_default_config(short_term, long_term, embedding_service)
    }

    #[tokio::test]
    async fn test_write_and_read() {
        let store = make_store().await;

        let key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };

        let request = make_write_request(key.clone(), 0.5);
        let entry = store.write(request).await.unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();

        assert!(read_result.is_some());
        let read_entry = read_result.unwrap();
        assert_eq!(read_entry.id, entry.id);
    }

    #[tokio::test]
    async fn test_delete() {
        let store = make_store().await;

        let key = MemoryKey::Global {
            key: "config".to_string(),
        };

        store
            .write(make_write_request(key.clone(), 0.5))
            .await
            .unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_some());

        store.delete(&key).await.unwrap();

        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_none());
    }

    #[tokio::test]
    async fn test_high_importance_writes_to_long_term() {
        let store = make_store().await;

        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };

        // Importance 0.9 > default threshold 0.7
        let request = make_write_request(key.clone(), 0.9);
        store.write(request).await.unwrap();

        // Verify short-term has it
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read_result.is_some());
    }

    #[tokio::test]
    async fn test_list_keys() {
        let store = make_store().await;

        let key1 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "a".to_string(),
        };
        let key2 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "b".to_string(),
        };

        store.write(make_write_request(key1, 0.5)).await.unwrap();
        store.write(make_write_request(key2, 0.5)).await.unwrap();

        let scope_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "".to_string(),
        };
        let keys = store.list_keys(&scope_key).await.unwrap();
        assert_eq!(keys.len(), 2);
    }

    #[tokio::test]
    async fn test_health_check() {
        let store = make_store().await;
        let health = store.health();
        assert_eq!(health.len(), 2);
        assert_eq!(health[0].name, "short_term_memory");
        assert_eq!(health[1].name, "long_term_memory");
        // Both should be in fallback mode
        assert_eq!(health[0].status, "fallback");
        assert_eq!(health[1].status, "fallback");
    }

    /// An explicitly configured in-memory short-term backend is healthy, not a
    /// degraded fallback (`UC_SHORT_TERM_BACKEND=memory`).
    #[tokio::test]
    async fn test_health_explicit_in_memory_is_healthy() {
        let short_term = Arc::new(ShortTermMemory::new_in_memory(3600));
        let long_term = Arc::new(LongTermMemory::new_fallback());
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        let store = MemoryStore::new_with_default_config(short_term, long_term, embedding_service);
        let health = store.health();
        let short = health
            .iter()
            .find(|c| c.name == "short_term_memory")
            .expect("short_term_memory component");
        assert_eq!(short.status, "ok");
        assert!(
            short
                .details
                .as_deref()
                .unwrap_or_default()
                .contains("explicitly configured"),
            "details should name the explicit configuration: {:?}",
            short.details
        );
    }

    #[tokio::test]
    async fn test_search_with_embedding() {
        let store = make_store().await;

        // Write an entry with embedding
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "arch".to_string(),
        };
        let request = MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text("architecture decision".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: Some(vec![1.0, 0.0, 0.0, 0.0]),
            },
            version: None,
        };
        store.write(request).await.unwrap();

        // Search with a similar embedding
        let results = store
            .search_with_embedding(
                vec![1.0, 0.0, 0.0, 0.0],
                MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
                10,
                0.0,
            )
            .await
            .unwrap();

        assert_eq!(results.len(), 1);
        assert!(results[0].score > 0.99);
    }

    // ── New tests for AC1, AC3, AC5 ──────────────────────────────

    /// AC1: search_memory returns valid results via BLAKE3 embedding
    #[tokio::test]
    async fn test_search_returns_results_with_blake3_embedding() {
        let store = make_store().await;

        // Write a high-importance entry — write() now auto-generates BLAKE3 embedding
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };
        let content_text = "system architecture decision";
        let request = MemoryWriteRequest {
            key: key.clone(),
            content: MemoryContent::Text(content_text.to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: None, // write() will auto-generate BLAKE3 embedding
            },
            version: None,
        };
        store.write(request).await.unwrap();

        // Search using the same content text — BLAKE3 embedding should produce
        // a matching vector for identical text
        let search_request = MemorySearchRequest {
            query: content_text.to_string(),
            scope: MemorySearchScope::Project {
                project_id: "p1".to_string(),
            },
            max_results: 10,
            min_score: 0.0,
        };

        let response = store.search(search_request).await.unwrap();
        assert!(
            !response.results.is_empty(),
            "search_memory should return results when using BLAKE3 embedding"
        );
        assert!(response.results[0].score > 0.99);
    }

    /// AC3: read() with include_semantic=true finds entries in long-term memory
    #[tokio::test]
    async fn test_read_include_semantic_finds_long_term_entry() {
        let store = make_store().await;

        // Write a high-importance entry with embedding directly to long-term memory
        // (bypass short-term so read() must use semantic search to find it)
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture decision".to_string(),
        };
        let query_text = "architecture decision";
        let embedding = store
            .embedding_service()
            .embed_single(query_text)
            .await
            .unwrap();

        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: key.clone(),
            content: MemoryContent::Text("Use microservices for scaling".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["architecture".to_string()],
                embedding: Some(embedding),
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            version: 0,
        };
        store.long_term().write(&entry).await.unwrap();

        // Read with include_semantic=true should find the entry
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: true,
            })
            .await
            .unwrap();

        assert!(
            read_result.is_some(),
            "read with include_semantic=true should find entry in long-term memory"
        );
        let found = read_result.unwrap();
        assert_eq!(found.key, key);
    }

    /// AC3: read() with include_semantic=false does NOT search long-term
    #[tokio::test]
    async fn test_read_without_semantic_skips_long_term() {
        let store = make_store().await;

        // Write a high-importance entry only to long-term
        let key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "design patterns".to_string(),
        };
        let embedding = store
            .embedding_service()
            .embed_single("design patterns")
            .await
            .unwrap();

        let entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: key.clone(),
            content: MemoryContent::Text("Use repository pattern".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.9,
                tags: vec!["patterns".to_string()],
                embedding: Some(embedding),
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
            version: 0,
        };
        store.long_term().write(&entry).await.unwrap();

        // Read with include_semantic=false should NOT find it
        let read_result = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: false,
            })
            .await
            .unwrap();

        assert!(
            read_result.is_none(),
            "read with include_semantic=false should not search long-term memory"
        );
    }

    /// AC5: search() gracefully handles embedding failures.
    ///
    /// Note: With the BLAKE3 fallback, `embed_single()` never fails (it's pure
    /// computation). The actual error path (Voyage API timeout / rate limit)
    /// can only be triggered with the `indexing` feature and a configured API
    /// key. This test verifies that search returns a valid response without
    /// panicking even when no results match (high min_score threshold).
    /// A full integration test with a failing Voyage API would be needed to
    /// exercise the `Err` branch of `embed_single()`.
    #[tokio::test]
    async fn test_search_graceful_degradation_on_embedding_failure() {
        let store = make_store().await;

        let search_request = MemorySearchRequest {
            query: "nonexistent query".to_string(),
            scope: MemorySearchScope::All,
            max_results: 10,
            min_score: 0.99, // Very high threshold to effectively get no results
        };

        let response = store.search(search_request).await.unwrap();
        // Should not error — graceful degradation
        assert!(
            response.results.is_empty(),
            "search with high min_score should return empty results without error"
        );
    }

    #[test]
    fn test_key_to_query_text() {
        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };
        assert_eq!(key_to_query_text(&task_key), "decisions");

        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        };
        assert_eq!(key_to_query_text(&project_key), "architecture");

        let global_key = MemoryKey::Global {
            key: "conventions".to_string(),
        };
        assert_eq!(key_to_query_text(&global_key), "conventions");
    }

    #[test]
    fn test_key_to_search_scope() {
        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "arch".to_string(),
        };
        let scope = key_to_search_scope(&project_key);
        assert!(matches!(
            scope,
            MemorySearchScope::Project { ref project_id } if project_id == "p1"
        ));

        let global_key = MemoryKey::Global {
            key: "conv".to_string(),
        };
        assert!(matches!(
            key_to_search_scope(&global_key),
            MemorySearchScope::Global
        ));

        let task_key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "dec".to_string(),
        };
        assert!(matches!(
            key_to_search_scope(&task_key),
            MemorySearchScope::All
        ));
    }

    #[test]
    fn test_content_to_text() {
        assert_eq!(
            content_to_text(&MemoryContent::Text("hello".to_string())),
            "hello"
        );
        let json: serde_json::Value = serde_json::json!({"key": "value"});
        assert!(content_to_text(&MemoryContent::Structured(json)).contains("value"));
        assert_eq!(
            content_to_text(&MemoryContent::Code {
                language: "rust".to_string(),
                code: "fn main() {}".to_string()
            }),
            "fn main() {}"
        );
        assert_eq!(
            content_to_text(&MemoryContent::Diff {
                file_path: "a.rs".to_string(),
                diff: "+hello".to_string()
            }),
            "+hello"
        );
        assert_eq!(
            content_to_text(&MemoryContent::Reference {
                uri: "https://example.com".to_string(),
                description: "external resource".to_string()
            }),
            "external resource"
        );
    }

    /// A duplicate replay delivery must not apply twice, even though the
    /// short-term entry it reads already carries the same version.
    #[tokio::test]
    async fn test_replay_write_skips_duplicate_version() {
        let store = make_store().await;
        let key = MemoryKey::Global {
            key: "dup".to_string(),
        };

        let first = store
            .replay_write(make_replay_request(key.clone(), "first", 100))
            .await
            .unwrap();
        assert!(first.applied);

        let duplicate = store
            .replay_write(make_replay_request(key.clone(), "duplicate", 100))
            .await
            .unwrap();
        assert!(!duplicate.applied, "an equal version is a duplicate replay");
        assert_eq!(duplicate.entry.version, 100);

        let read = store
            .read(MemoryReadRequest {
                key,
                include_semantic: false,
            })
            .await
            .unwrap()
            .expect("entry should exist");
        match read.content {
            MemoryContent::Text(t) => assert_eq!(t, "first"),
            _ => panic!("expected text content"),
        }
    }

    /// A stale replay that races a delete must not resurrect the key, and the
    /// skip path must return a committed state (the tombstone) rather than
    /// panicking when no replay high-water mark exists yet.
    #[tokio::test]
    async fn test_replay_write_after_delete_does_not_resurrect_key() {
        let store = make_store().await;
        let key = MemoryKey::Global {
            key: "deleted-then-replay".to_string(),
        };
        store
            .write(make_write_request(key.clone(), 0.5))
            .await
            .unwrap();
        store.delete(&key).await.unwrap();

        let result = store
            .replay_write(make_replay_request(key.clone(), "stale", 1))
            .await
            .unwrap();

        assert!(!result.applied, "an old replay must not undo the delete");
        let read = store
            .read(MemoryReadRequest {
                key,
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(read.is_none(), "the key stays deleted");
    }

    /// Deleting leaves a tombstone: short-term reads, semantic reads and
    /// fabricated backend hits all stop serving the entry, and a later write
    /// revives the key.
    #[tokio::test]
    async fn test_delete_tombstone_blocks_reads_and_search() {
        let store = make_store().await;
        let key = MemoryKey::Global {
            key: "tombstoned".to_string(),
        };
        let entry = store
            .write(make_write_request(key.clone(), 1.0))
            .await
            .unwrap();

        store.delete(&key).await.unwrap();

        let read = store
            .read(MemoryReadRequest {
                key: key.clone(),
                include_semantic: true,
            })
            .await
            .unwrap();
        assert!(read.is_none(), "a deleted key must read as absent");
        assert!(store.is_deleted(&key).await.unwrap());

        // Even if the vector backend still returns the deleted entry, the
        // read-side filter must drop it.
        let kept = store
            .retain_recallable(
                vec![MemorySearchResult { entry, score: 0.9 }],
                &MemorySearchScope::All,
            )
            .await
            .unwrap();
        assert!(kept.is_empty(), "tombstoned hits must not be recallable");

        // A new write revives the key (mark-not-delete, not permanent loss).
        store
            .write(make_write_request(key.clone(), 0.5))
            .await
            .unwrap();
        assert!(!store.is_deleted(&key).await.unwrap());
        let revived = store
            .read(MemoryReadRequest {
                key,
                include_semantic: false,
            })
            .await
            .unwrap();
        assert!(revived.is_some(), "the revived key must be readable again");
    }

    /// Recall filters drop out-of-scope and expired entries, and keep the
    /// matching one — the long-term backend is intentionally permissive.
    #[tokio::test]
    async fn test_retain_recallable_filters_scope_and_ttl() {
        let store = make_store().await;
        let project_a = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "match".to_string(),
        };
        let project_b = MemoryKey::Project {
            project_id: "p2".to_string(),
            key: "other".to_string(),
        };
        let keep = store
            .write(make_write_request(project_a, 0.5))
            .await
            .unwrap();
        let drop = store
            .write(make_write_request(project_b, 0.5))
            .await
            .unwrap();
        let mut expired = store
            .write(make_write_request(
                MemoryKey::Project {
                    project_id: "p1".to_string(),
                    key: "expired".to_string(),
                },
                0.5,
            ))
            .await
            .unwrap();
        expired.metadata.tags.push(format!("{}1", TTL_TAG_PREFIX));
        expired.updated_at = chrono::Utc::now() - chrono::Duration::seconds(30);

        let kept = store
            .retain_recallable(
                vec![
                    MemorySearchResult {
                        entry: keep.clone(),
                        score: 0.9,
                    },
                    MemorySearchResult {
                        entry: drop,
                        score: 0.9,
                    },
                    MemorySearchResult {
                        entry: expired,
                        score: 0.9,
                    },
                ],
                &MemorySearchScope::Project {
                    project_id: "p1".to_string(),
                },
            )
            .await
            .unwrap();
        assert_eq!(kept.len(), 1, "only the in-scope, unexpired hit survives");
        assert_eq!(kept[0].entry.id, keep.id);
    }

    #[test]
    fn test_ttl_tag_expiry() {
        let now = chrono::Utc::now();
        let mut entry = MemoryEntry {
            id: uc_types::memory::MemoryId::new(),
            key: MemoryKey::Global {
                key: "ttl".to_string(),
            },
            content: MemoryContent::Text("x".to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec![],
                embedding: None,
            },
            created_at: now - chrono::Duration::seconds(120),
            updated_at: now - chrono::Duration::seconds(120),
            version: 1,
        };
        assert!(!ttl_expired(&entry, now), "no ttl tag never expires");

        entry.metadata.tags.push(format!("{}60", TTL_TAG_PREFIX));
        assert!(
            ttl_expired(&entry, now),
            "ttl:60 of an old entry is expired"
        );

        entry.metadata.tags = vec![format!("{}not-a-number", TTL_TAG_PREFIX)];
        assert!(!ttl_expired(&entry, now), "malformed ttl must not delete");

        entry.metadata.tags = vec![format!(
            "{}{}",
            EXPIRES_AT_TAG_PREFIX,
            (now - chrono::Duration::seconds(5)).to_rfc3339()
        )];
        assert!(ttl_expired(&entry, now), "past expires_at is expired");
    }

    #[test]
    fn test_key_scope_and_search_scope_matching() {
        let global = MemoryKey::Global {
            key: "g".to_string(),
        };
        let project_a = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "a".to_string(),
        };
        let project_b = MemoryKey::Project {
            project_id: "p2".to_string(),
            key: "b".to_string(),
        };
        let task_a = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "a".to_string(),
        };

        assert!(key_scope_matches(&global, &global));
        assert!(key_scope_matches(&project_a, &project_a));
        assert!(!key_scope_matches(&project_a, &project_b));
        assert!(!key_scope_matches(&project_a, &global));
        assert!(key_scope_matches(&task_a, &task_a));

        assert!(scope_allows(&MemorySearchScope::All, &global));
        assert!(scope_allows(
            &MemorySearchScope::Project {
                project_id: "p1".to_string()
            },
            &project_a
        ));
        assert!(!scope_allows(
            &MemorySearchScope::Project {
                project_id: "p1".to_string()
            },
            &project_b
        ));
        assert!(!scope_allows(&MemorySearchScope::Global, &project_a));
    }
}
