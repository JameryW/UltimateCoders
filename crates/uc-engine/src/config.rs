//! Engine configuration — storage endpoints, feature flags, and tuning parameters.
//!
//! Supports loading from environment variables and TOML config files.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Top-level engine configuration.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct EngineConfig {
    /// Storage configuration.
    pub storage: StorageConfig,
    /// Memory configuration.
    pub memory: MemoryConfig,
    /// Embedding configuration.
    pub embedding: EmbeddingConfig,
}

impl EngineConfig {
    /// Load configuration from environment variables.
    ///
    /// Env vars:
    /// - `UC_TIKV_ENDPOINT`: PD endpoint for TiKV (default: "127.0.0.1:2379")
    /// - `UC_QDRANT_URL`: Qdrant gRPC URL (default: "http://127.0.0.1:6334")
    /// - `UC_PG_URL`: PostgreSQL connection string (default: "postgres://localhost/ultimatecoders")
    /// - `UC_NATS_URL`: NATS server URL (default: "nats://127.0.0.1:4222")
    pub fn from_env() -> Self {
        Self {
            storage: StorageConfig::from_env(),
            memory: MemoryConfig::default(),
            embedding: EmbeddingConfig::from_env(),
        }
    }

    /// Load configuration from a TOML file.
    pub fn from_toml(path: &std::path::Path) -> Result<Self, crate::config::ConfigError> {
        let content =
            std::fs::read_to_string(path).map_err(|e| ConfigError::IoError(e.to_string()))?;
        let config: EngineConfig =
            toml::from_str(&content).map_err(|e| ConfigError::ParseError(e.to_string()))?;
        Ok(config)
    }
}

/// Storage configuration for all backend services.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StorageConfig {
    /// TiKV PD endpoints.
    pub tikv_pd_endpoints: Vec<String>,
    /// Qdrant gRPC URL.
    pub qdrant_url: String,
    /// Optional Qdrant API key.
    pub qdrant_api_key: Option<String>,
    /// PostgreSQL connection string.
    pub pg_url: String,
    /// NATS server URL.
    pub nats_url: String,
    /// Connection timeout for all storage clients.
    pub connect_timeout: Duration,
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            tikv_pd_endpoints: vec!["127.0.0.1:2379".into()],
            qdrant_url: "http://127.0.0.1:6334".into(),
            qdrant_api_key: None,
            pg_url: "postgres://localhost/ultimatecoders".into(),
            nats_url: "nats://127.0.0.1:4222".into(),
            connect_timeout: Duration::from_secs(10),
        }
    }
}

impl StorageConfig {
    /// Load storage config from environment variables.
    pub fn from_env() -> Self {
        Self {
            tikv_pd_endpoints: std::env::var("UC_TIKV_ENDPOINT")
                .ok()
                .map(|e| {
                    e.split(',')
                        .map(|s| s.trim().to_string())
                        .collect::<Vec<_>>()
                })
                .unwrap_or_else(|| vec!["127.0.0.1:2379".into()]),
            qdrant_url: std::env::var("UC_QDRANT_URL")
                .unwrap_or_else(|_| "http://127.0.0.1:6334".into()),
            qdrant_api_key: std::env::var("UC_QDRANT_API_KEY").ok(),
            pg_url: std::env::var("UC_PG_URL")
                .unwrap_or_else(|_| "postgres://localhost/ultimatecoders".into()),
            nats_url: std::env::var("UC_NATS_URL")
                .unwrap_or_else(|_| "nats://127.0.0.1:4222".into()),
            connect_timeout: Duration::from_secs(10),
        }
    }
}

/// Memory configuration — importance threshold and TTL settings.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryConfig {
    /// Importance threshold above which entries are also stored in long-term memory.
    pub long_term_importance_threshold: f32,
    /// Default TTL for task-scoped short-term memory entries (seconds).
    pub task_ttl_seconds: u64,
    /// Maximum number of results returned by a memory search.
    pub max_search_results: u32,
    /// Minimum similarity score for memory search results.
    pub min_search_score: f32,
}

impl Default for MemoryConfig {
    fn default() -> Self {
        Self {
            long_term_importance_threshold: 0.7,
            task_ttl_seconds: 3600, // 1 hour
            max_search_results: 20,
            min_search_score: 0.5,
        }
    }
}

/// Configuration error types.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("IO error: {0}")]
    IoError(String),
    #[error("Parse error: {0}")]
    ParseError(String),
}

/// Embedding configuration for the semantic search pipeline.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbeddingConfig {
    /// Voyage AI API key (required for live embedding).
    pub voyage_api_key: Option<String>,
    /// Embedding model name.
    pub model: String,
    /// Embedding vector dimensions.
    pub dimensions: usize,
    /// Maximum batch size per embedding API call.
    pub batch_size: usize,
    /// Maximum number of retries on rate-limit errors.
    pub max_retries: u32,
    /// Base delay in milliseconds for exponential backoff.
    pub retry_base_delay_ms: u64,
    /// Maximum delay in milliseconds for backoff.
    pub retry_max_delay_ms: u64,
}

impl Default for EmbeddingConfig {
    fn default() -> Self {
        Self {
            voyage_api_key: None,
            model: "voyage-code-3".to_string(),
            dimensions: 1024,
            batch_size: 128,
            max_retries: 5,
            retry_base_delay_ms: 1000,
            retry_max_delay_ms: 60000,
        }
    }
}

impl EmbeddingConfig {
    /// Load embedding configuration from environment variables.
    ///
    /// Env vars:
    /// - `UC_VOYAGE_API_KEY`: Voyage AI API key
    /// - `UC_EMBEDDING_MODEL`: Model name (default: "voyage-code-3")
    /// - `UC_EMBEDDING_DIMENSIONS`: Vector dimensions (default: 1024)
    /// - `UC_EMBEDDING_BATCH_SIZE`: Batch size per API call (default: 128)
    pub fn from_env() -> Self {
        Self {
            voyage_api_key: std::env::var("UC_VOYAGE_API_KEY").ok(),
            model: std::env::var("UC_EMBEDDING_MODEL")
                .unwrap_or_else(|_| "voyage-code-3".to_string()),
            dimensions: std::env::var("UC_EMBEDDING_DIMENSIONS")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(1024),
            batch_size: std::env::var("UC_EMBEDDING_BATCH_SIZE")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(128),
            max_retries: 5,
            retry_base_delay_ms: 1000,
            retry_max_delay_ms: 60000,
        }
    }
}
