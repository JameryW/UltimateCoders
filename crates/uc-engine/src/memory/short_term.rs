//! Short-term memory store backed by TiKV.
//!
//! Stores task-scoped context (code change diffs, decision records, progress state).
//! Keys are encoded as `memory:{scope}:{scope_id}:{key}` for prefix scanning.
//! Entries support TTL for automatic expiry of task-scoped data.

use uc_types::error::EngineError;
use uc_types::memory::{MemoryContent, MemoryEntry, MemoryId, MemoryKey, MemoryMetadata};

use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::RwLock;

/// Internal representation of a short-term memory value stored in TiKV.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct StoredEntry {
    id: String,
    content: MemoryContent,
    metadata: MemoryMetadata,
    created_at: String,
    updated_at: String,
}

/// Short-term memory store backed by TiKV (raw KV mode).
///
/// Uses TiKV's raw client for simple get/put/delete/scan operations.
/// Keys are encoded with a structured prefix for efficient prefix scanning.
pub struct ShortTermMemory {
    #[cfg(feature = "storage")]
    client: Option<Arc<tikv_client::RawClient>>,
    /// In-memory fallback when TiKV is not available (for testing / local dev).
    fallback: Arc<RwLock<Vec<(String, StoredEntry)>>>,
    /// Default TTL for task-scoped entries (in seconds). 0 means no TTL.
    default_ttl_seconds: u64,
}

impl ShortTermMemory {
    /// Create a new short-term memory store with a TiKV client.
    #[cfg(feature = "storage")]
    pub async fn new(pd_endpoints: Vec<String>, ttl_seconds: u64) -> Result<Self, EngineError> {
        match tikv_client::RawClient::new(pd_endpoints).await {
            Ok(client) => {
                let client_arc = Arc::new(client);
                // ponytail: verify TiKV store is writable — PD can be up while TiKV
                // nodes are still starting. Retry up to 3 times with 2s backoff.
                let mut probe_ok = false;
                for attempt in 0..3 {
                    let probe_key = format!("__uc_probe_{}", uuid::Uuid::new_v4());
                    match client_arc.put(probe_key.clone(), b"probe".to_vec()).await {
                        Ok(_) => {
                            let _ = client_arc.delete(probe_key).await;
                            probe_ok = true;
                            break;
                        }
                        Err(e) if attempt < 2 => {
                            tracing::warn!(
                                "TiKV write probe failed (attempt {}): {}, retrying in 2s",
                                attempt + 1,
                                e
                            );
                            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        }
                        Err(e) => {
                            tracing::warn!("TiKV write probe failed after 3 attempts: {}", e);
                        }
                    }
                }

                if probe_ok {
                    tracing::info!("Connected to TiKV for short-term memory");
                    Ok(Self {
                        client: Some(client_arc),
                        fallback: Arc::new(RwLock::new(Vec::new())),
                        default_ttl_seconds: ttl_seconds,
                    })
                } else {
                    tracing::warn!("TiKV PD reachable but store not ready, using in-memory fallback for short-term memory");
                    Ok(Self {
                        client: None,
                        fallback: Arc::new(RwLock::new(Vec::new())),
                        default_ttl_seconds: ttl_seconds,
                    })
                }
            }
            Err(e) => {
                tracing::warn!(
                    "TiKV unavailable, using in-memory fallback for short-term memory: {}",
                    e
                );
                Ok(Self {
                    client: None,
                    fallback: Arc::new(RwLock::new(Vec::new())),
                    default_ttl_seconds: ttl_seconds,
                })
            }
        }
    }

    /// Create a new short-term memory store (fallback-only when storage feature is disabled).
    #[cfg(not(feature = "storage"))]
    pub async fn new(_pd_endpoints: Vec<String>, ttl_seconds: u64) -> Result<Self, EngineError> {
        tracing::info!("Storage feature disabled, using in-memory fallback for short-term memory");
        Ok(Self::new_fallback(ttl_seconds))
    }

    /// Create with an existing TiKV client (for testing / dependency injection).
    #[cfg(feature = "storage")]
    pub fn with_client(client: Arc<tikv_client::RawClient>, ttl_seconds: u64) -> Self {
        Self {
            client: Some(client),
            fallback: Arc::new(RwLock::new(Vec::new())),
            default_ttl_seconds: ttl_seconds,
        }
    }

    /// Create with in-memory fallback only (for testing).
    pub fn new_fallback(ttl_seconds: u64) -> Self {
        Self {
            #[cfg(feature = "storage")]
            client: None,
            fallback: Arc::new(RwLock::new(Vec::new())),
            default_ttl_seconds: ttl_seconds,
        }
    }

    /// Read a memory entry by key.
    pub async fn read(&self, key: &MemoryKey) -> Result<Option<MemoryEntry>, EngineError> {
        let encoded_key = encode_key(key);

        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            let value = client
                .get(encoded_key.clone())
                .await
                .map_err(|e| EngineError::MemoryReadError(format!("TiKV read error: {}", e)))?;

            match value {
                Some(bytes) => {
                    let stored: StoredEntry = serde_json::from_slice(&bytes).map_err(|e| {
                        EngineError::MemoryReadError(format!("Deserialization error: {}", e))
                    })?;
                    Ok(Some(stored.to_entry(key.clone())))
                }
                None => Ok(None),
            }
        } else {
            let fallback = self.fallback.read().await;
            Ok(fallback
                .iter()
                .find(|(k, _)| k == &encoded_key)
                .map(|(_, v)| v.to_entry(key.clone())))
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            Ok(fallback
                .iter()
                .find(|(k, _)| k == &encoded_key)
                .map(|(_, v)| v.to_entry(key.clone())))
        }
    }

    /// Write a memory entry.
    pub async fn write(&self, entry: &MemoryEntry) -> Result<(), EngineError> {
        let encoded_key = encode_key(&entry.key);
        let stored = StoredEntry::from_entry(entry);
        let value = serde_json::to_vec(&stored)
            .map_err(|e| EngineError::MemoryWriteError(format!("Serialization error: {}", e)))?;

        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            client
                .put(encoded_key.clone(), value)
                .await
                .map_err(|e| EngineError::MemoryWriteError(format!("TiKV write error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.iter_mut().find(|(k, _)| k == &encoded_key) {
                existing.1 = stored;
            } else {
                fallback.push((encoded_key, stored));
            }
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            if let Some(existing) = fallback.iter_mut().find(|(k, _)| k == &encoded_key) {
                existing.1 = stored;
            } else {
                fallback.push((encoded_key, stored));
            }
            let _ = value; // suppress unused warning
        }

        Ok(())
    }

    /// Delete a memory entry by key.
    pub async fn delete(&self, key: &MemoryKey) -> Result<(), EngineError> {
        let encoded_key = encode_key(key);

        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            client
                .delete(encoded_key)
                .await
                .map_err(|e| EngineError::MemoryWriteError(format!("TiKV delete error: {}", e)))?;
        } else {
            let mut fallback = self.fallback.write().await;
            fallback.retain(|(k, _)| k != &encoded_key);
        }

        #[cfg(not(feature = "storage"))]
        {
            let mut fallback = self.fallback.write().await;
            fallback.retain(|(k, _)| k != &encoded_key);
        }

        Ok(())
    }

    /// List all keys matching a given prefix.
    ///
    /// The prefix should be a scope prefix like `memory:task:abc123:`.
    pub async fn list_keys(&self, prefix: &str) -> Result<Vec<MemoryKey>, EngineError> {
        #[cfg(feature = "storage")]
        if let Some(client) = &self.client {
            let start_key = prefix.to_string();
            // Scan range: prefix to prefix + 0xFF (next byte after prefix)
            let end_key = prefix.to_string() + "\u{00ff}";
            let pairs = client
                .scan(tikv_client::BoundRange::from(start_key..end_key), 1024)
                .await
                .map_err(|e| EngineError::MemoryReadError(format!("TiKV scan error: {}", e)))?;

            let keys = pairs
                .into_iter()
                .filter_map(|pair| {
                    let key_bytes: Vec<u8> = pair.into_key().into();
                    let key_str = String::from_utf8_lossy(&key_bytes).to_string();
                    decode_key(&key_str)
                })
                .collect();
            Ok(keys)
        } else {
            let fallback = self.fallback.read().await;
            let keys = fallback
                .iter()
                .filter(|(k, _)| k.starts_with(prefix))
                .filter_map(|(k, _)| decode_key(k))
                .collect();
            Ok(keys)
        }

        #[cfg(not(feature = "storage"))]
        {
            let fallback = self.fallback.read().await;
            let keys = fallback
                .iter()
                .filter(|(k, _)| k.starts_with(prefix))
                .filter_map(|(k, _)| decode_key(k))
                .collect();
            Ok(keys)
        }
    }

    /// Check if the external storage backend (TiKV) is connected.
    ///
    /// Returns `false` for in-memory fallback mode — the store is
    /// **functional** but not connected to a persistent backend.
    /// Use this to distinguish "connected to TiKV" from "using fallback"
    /// in health check reporting.
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

    /// Get default TTL in seconds.
    pub fn default_ttl(&self) -> u64 {
        self.default_ttl_seconds
    }
}

/// Encode a MemoryKey into a TiKV key string.
///
/// Format: `memory:{scope}:{scope_id}:{key}`
/// - Task scope: `memory:task:{task_id}:{key}`
/// - Project scope: `memory:project:{project_id}:{key}`
/// - Global scope: `memory:global:{key}`
pub fn encode_key(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task {
            task_id,
            key: inner,
        } => {
            format!("memory:task:{}:{}", task_id, inner)
        }
        MemoryKey::Project {
            project_id,
            key: inner,
        } => {
            format!("memory:project:{}:{}", project_id, inner)
        }
        MemoryKey::Global { key: inner } => {
            format!("memory:global:{}", inner)
        }
    }
}

/// Decode a TiKV key string back into a MemoryKey.
///
/// Returns None if the key format is unrecognized.
pub fn decode_key(encoded: &str) -> Option<MemoryKey> {
    let parts: Vec<&str> = encoded.splitn(4, ':').collect();
    if parts.len() < 3 || parts[0] != "memory" {
        return None;
    }

    match parts[1] {
        "task" if parts.len() == 4 => Some(MemoryKey::Task {
            task_id: parts[2].to_string(),
            key: parts[3].to_string(),
        }),
        "project" if parts.len() == 4 => Some(MemoryKey::Project {
            project_id: parts[2].to_string(),
            key: parts[3].to_string(),
        }),
        "global" if parts.len() == 3 => Some(MemoryKey::Global {
            key: parts[2].to_string(),
        }),
        _ => None,
    }
}

/// Get the prefix for scanning all entries in a scope.
pub fn scope_prefix(key: &MemoryKey) -> String {
    match key {
        MemoryKey::Task { task_id, .. } => format!("memory:task:{}:", task_id),
        MemoryKey::Project { project_id, .. } => format!("memory:project:{}:", project_id),
        MemoryKey::Global { .. } => "memory:global:".to_string(),
    }
}

impl StoredEntry {
    fn from_entry(entry: &MemoryEntry) -> Self {
        Self {
            id: entry.id.0.clone(),
            content: entry.content.clone(),
            metadata: entry.metadata.clone(),
            created_at: entry.created_at.to_rfc3339(),
            updated_at: entry.updated_at.to_rfc3339(),
        }
    }

    fn to_entry(&self, key: MemoryKey) -> MemoryEntry {
        MemoryEntry {
            id: MemoryId(self.id.clone()),
            key,
            content: self.content.clone(),
            metadata: self.metadata.clone(),
            created_at: self.created_at.parse().unwrap_or(chrono::Utc::now()),
            updated_at: self.updated_at.parse().unwrap_or(chrono::Utc::now()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::memory::{MemoryContent, MemoryMetadata};

    fn make_entry(key: MemoryKey, content: &str) -> MemoryEntry {
        MemoryEntry {
            id: MemoryId::new(),
            key,
            content: MemoryContent::Text(content.to_string()),
            metadata: MemoryMetadata {
                source_agent: "test".to_string(),
                importance: 0.5,
                tags: vec!["test".to_string()],
                embedding: None,
            },
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        }
    }

    #[test]
    fn test_encode_decode_task_key() {
        let key = MemoryKey::Task {
            task_id: "abc123".to_string(),
            key: "decisions".to_string(),
        };
        let encoded = encode_key(&key);
        assert_eq!(encoded, "memory:task:abc123:decisions");
        let decoded = decode_key(&encoded).unwrap();
        assert_eq!(decoded, key);
    }

    #[test]
    fn test_encode_decode_project_key() {
        let key = MemoryKey::Project {
            project_id: "proj1".to_string(),
            key: "architecture".to_string(),
        };
        let encoded = encode_key(&key);
        assert_eq!(encoded, "memory:project:proj1:architecture");
        let decoded = decode_key(&encoded).unwrap();
        assert_eq!(decoded, key);
    }

    #[test]
    fn test_encode_decode_global_key() {
        let key = MemoryKey::Global {
            key: "conventions".to_string(),
        };
        let encoded = encode_key(&key);
        assert_eq!(encoded, "memory:global:conventions");
        let decoded = decode_key(&encoded).unwrap();
        assert_eq!(decoded, key);
    }

    #[test]
    fn test_decode_invalid_key() {
        assert!(decode_key("invalid:key").is_none());
        assert!(decode_key("memory:unknown:foo").is_none());
    }

    #[test]
    fn test_scope_prefix() {
        let task_key = MemoryKey::Task {
            task_id: "abc".to_string(),
            key: "x".to_string(),
        };
        assert_eq!(scope_prefix(&task_key), "memory:task:abc:");

        let project_key = MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "y".to_string(),
        };
        assert_eq!(scope_prefix(&project_key), "memory:project:p1:");

        let global_key = MemoryKey::Global {
            key: "z".to_string(),
        };
        assert_eq!(scope_prefix(&global_key), "memory:global:");
    }

    #[tokio::test]
    async fn test_fallback_read_write_delete() {
        let store = ShortTermMemory::new_fallback(3600);

        let key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };
        let entry = make_entry(key.clone(), "Use PostgreSQL for metadata");

        // Write
        store.write(&entry).await.unwrap();

        // Read
        let result = store.read(&key).await.unwrap();
        assert!(result.is_some());
        let read_entry = result.unwrap();
        assert_eq!(read_entry.id, entry.id);
        if let MemoryContent::Text(text) = &read_entry.content {
            assert_eq!(text, "Use PostgreSQL for metadata");
        } else {
            panic!("Expected Text content");
        }

        // Delete
        store.delete(&key).await.unwrap();
        let result = store.read(&key).await.unwrap();
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn test_fallback_list_keys() {
        let store = ShortTermMemory::new_fallback(3600);

        let key1 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        };
        let key2 = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "progress".to_string(),
        };
        let key3 = MemoryKey::Task {
            task_id: "t2".to_string(),
            key: "decisions".to_string(),
        };

        store.write(&make_entry(key1, "a")).await.unwrap();
        store.write(&make_entry(key2, "b")).await.unwrap();
        store.write(&make_entry(key3, "c")).await.unwrap();

        let prefix = "memory:task:t1:";
        let keys = store.list_keys(prefix).await.unwrap();
        assert_eq!(keys.len(), 2);
    }

    #[tokio::test]
    async fn test_fallback_overwrite() {
        let store = ShortTermMemory::new_fallback(3600);

        let key = MemoryKey::Global {
            key: "config".to_string(),
        };
        store.write(&make_entry(key.clone(), "v1")).await.unwrap();
        store.write(&make_entry(key.clone(), "v2")).await.unwrap();

        let result = store.read(&key).await.unwrap().unwrap();
        if let MemoryContent::Text(text) = &result.content {
            assert_eq!(text, "v2");
        } else {
            panic!("Expected Text content");
        }
    }

    #[test]
    fn test_stored_entry_roundtrip() {
        let entry = make_entry(
            MemoryKey::Task {
                task_id: "t1".to_string(),
                key: "test".to_string(),
            },
            "hello",
        );
        let stored = StoredEntry::from_entry(&entry);
        let restored = stored.to_entry(entry.key.clone());
        assert_eq!(restored.id, entry.id);
        assert_eq!(restored.key, entry.key);
    }
}
