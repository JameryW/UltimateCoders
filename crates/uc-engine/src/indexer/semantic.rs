//! Semantic-level indexing (code embedding -> Qdrant).
//!
//! Provides an `EmbeddingService` that computes code embeddings via the
//! Voyage Code 3 API (or a deterministic BLAKE3-based fallback for testing).
//! Also provides `SemanticIndexer` which takes AST-extracted symbol chunks,
//! embeds them, and upserts them into Qdrant for vector similarity search.

use uc_types::error::EngineError;
use uc_types::index::{ChunkType, CodeChunk};

use crate::config::EmbeddingConfig;

use std::sync::Arc;
use tokio::sync::RwLock;

/// Qdrant collection name for code embeddings.
#[allow(dead_code)]
const CODE_EMBEDDINGS_COLLECTION: &str = "code_embeddings";

// ── Embedding Service ────────────────────────────────────────────

/// Service for computing code embeddings.
///
/// When a Voyage AI API key is configured, calls the Voyage Code 3 API.
/// Otherwise, falls back to a deterministic BLAKE3-based embedding that
/// produces consistent vectors without network access (for testing / offline).
pub struct EmbeddingService {
    #[cfg(feature = "indexing")]
    http_client: reqwest::Client,
    config: EmbeddingConfig,
    /// Local cache: content_hash -> embedding vector.
    /// Avoids re-embedding unchanged content.
    cache: Arc<RwLock<std::collections::HashMap<String, Vec<f32>>>>,
    /// Whether the live API is available (has API key).
    api_available: bool,
}

impl EmbeddingService {
    /// Create a new embedding service with the given configuration.
    pub fn new(config: EmbeddingConfig) -> Self {
        let api_available = config.voyage_api_key.is_some();

        Self {
            #[cfg(feature = "indexing")]
            http_client: reqwest::Client::new(),
            config,
            cache: Arc::new(RwLock::new(std::collections::HashMap::new())),
            api_available,
        }
    }

    /// Create with fallback-only embedding (no API key, BLAKE3-based).
    pub fn new_fallback() -> Self {
        Self::new(EmbeddingConfig::default())
    }

    /// Embed a single text string.
    pub async fn embed_single(&self, text: &str) -> Result<Vec<f32>, EngineError> {
        let results = self.embed_batch(std::slice::from_ref(&text.to_string())).await?;
        Ok(results.into_iter().next().unwrap_or_else(|| vec![0.0f32; self.config.dimensions]))
    }

    /// Embed a batch of text strings.
    ///
    /// Uses the Voyage AI API when available, otherwise falls back to
    /// deterministic BLAKE3-based embeddings.
    pub async fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, EngineError> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }

        // Check cache first — if all texts are cached, return immediately
        {
            let cache = self.cache.read().await;
            let mut all_cached = true;
            for text in texts {
                let hash = blake3::hash(text.as_bytes()).to_hex().to_string();
                if !cache.contains_key(&hash) {
                    all_cached = false;
                    break;
                }
            }
            if all_cached {
                let mut results = Vec::with_capacity(texts.len());
                for text in texts {
                    let hash = blake3::hash(text.as_bytes()).to_hex().to_string();
                    results.push(cache.get(&hash).cloned().unwrap());
                }
                return Ok(results);
            }
        }

        // Compute embeddings for uncached texts
        let raw_embeddings = if self.api_available {
            #[cfg(feature = "indexing")]
            {
                self.embed_batch_via_api(texts).await?
            }
            #[cfg(not(feature = "indexing"))]
            {
                self.embed_batch_fallback(texts)
            }
        } else {
            self.embed_batch_fallback(texts)
        };

        // Store in cache and build final results
        let mut cache = self.cache.write().await;
        let mut results = Vec::with_capacity(texts.len());
        for (i, text) in texts.iter().enumerate() {
            let hash = blake3::hash(text.as_bytes()).to_hex().to_string();
            let embedding = if i < raw_embeddings.len() {
                cache.insert(hash.clone(), raw_embeddings[i].clone());
                raw_embeddings[i].clone()
            } else {
                vec![0.0f32; self.config.dimensions]
            };
            results.push(embedding);
        }

        Ok(results)
    }

    /// Get the configured embedding dimensions.
    pub fn dimensions(&self) -> usize {
        self.config.dimensions
    }

    /// Check if the live API is available.
    pub fn is_api_available(&self) -> bool {
        self.api_available
    }

    /// Get the model name.
    pub fn model_name(&self) -> &str {
        &self.config.model
    }

    /// Get the max batch size.
    pub fn batch_size(&self) -> usize {
        self.config.batch_size
    }

    // ── Voyage AI API implementation ─────────────────────────────

    /// Call the Voyage AI embeddings API with retry logic.
    #[cfg(feature = "indexing")]
    async fn embed_batch_via_api(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, EngineError> {
        let api_key = self
            .config
            .voyage_api_key
            .as_ref()
            .ok_or_else(|| EngineError::ConfigError("Voyage API key not configured".into()))?;

        // Process in batches of config.batch_size
        let mut all_embeddings = Vec::new();
        let batch_size = self.config.batch_size;

        for chunk in texts.chunks(batch_size) {
            let embeddings = self
                .call_voyage_api_with_retry(api_key, chunk)
                .await?;
            all_embeddings.extend(embeddings);
        }

        Ok(all_embeddings)
    }

    /// Call Voyage AI API with exponential backoff retry.
    #[cfg(feature = "indexing")]
    async fn call_voyage_api_with_retry(
        &self,
        api_key: &str,
        texts: &[String],
    ) -> Result<Vec<Vec<f32>>, EngineError> {
        let mut attempt = 0u32;
        let base_delay = std::time::Duration::from_millis(self.config.retry_base_delay_ms);
        let max_delay = std::time::Duration::from_millis(self.config.retry_max_delay_ms);

        loop {
            match self.call_voyage_api_once(api_key, texts).await {
                Ok(result) => return Ok(result),
                Err(EngineError::RateLimited(retry_after)) => {
                    if attempt >= self.config.max_retries {
                        return Err(EngineError::RateLimited(retry_after));
                    }
                    let delay = std::time::Duration::from_secs(retry_after);
                    tracing::warn!(
                        "Rate limited by Voyage AI, retrying in {:?} (attempt {}/{})",
                        delay, attempt + 1, self.config.max_retries
                    );
                    tokio::time::sleep(delay).await;
                    attempt += 1;
                }
                Err(e) if e.is_retryable() => {
                    if attempt >= self.config.max_retries {
                        return Err(e);
                    }
                    let exp_delay = base_delay * 2u32.pow(attempt);
                    let jitter = rand_jitter_ms();
                    let delay = std::cmp::min(exp_delay + std::time::Duration::from_millis(jitter), max_delay);
                    tracing::warn!(
                        "Retryable error from Voyage AI: {}, retrying in {:?} (attempt {}/{})",
                        e, delay, attempt + 1, self.config.max_retries
                    );
                    tokio::time::sleep(delay).await;
                    attempt += 1;
                }
                Err(e) => return Err(e),
            }
        }
    }

    /// Single call to the Voyage AI embeddings API.
    #[cfg(feature = "indexing")]
    async fn call_voyage_api_once(
        &self,
        api_key: &str,
        texts: &[String],
    ) -> Result<Vec<Vec<f32>>, EngineError> {
        #[derive(serde::Serialize)]
        struct EmbeddingRequest {
            model: String,
            input: Vec<String>,
            input_type: String,
        }

        #[derive(serde::Deserialize)]
        struct EmbeddingResponse {
            data: Vec<EmbeddingData>,
        }

        #[derive(serde::Deserialize)]
        struct EmbeddingData {
            embedding: Vec<f32>,
        }

        let request_body = EmbeddingRequest {
            model: self.config.model.clone(),
            input: texts.to_vec(),
            input_type: "document".to_string(),
        };

        let response = self
            .http_client
            .post("https://api.voyageai.com/v1/embeddings")
            .header("Authorization", format!("Bearer {}", api_key))
            .json(&request_body)
            .send()
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Voyage AI request error: {}", e)))?;

        let status = response.status();
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            let retry_after = response
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse().ok())
                .unwrap_or(5);
            return Err(EngineError::RateLimited(retry_after));
        }

        if status.is_server_error() {
            return Err(EngineError::ConnectionError(
                format!("Voyage AI server error: {}", status),
            ));
        }

        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(EngineError::SearchError(
                format!("Voyage API error ({}): {}", status, body),
            ));
        }

        let embedding_response: EmbeddingResponse = response
            .json()
            .await
            .map_err(|e| EngineError::SearchError(format!("Voyage AI response parse error: {}", e)))?;

        Ok(embedding_response
            .data
            .into_iter()
            .map(|d| d.embedding)
            .collect())
    }

    // ── Fallback embedding (BLAKE3-based, deterministic) ─────────

    /// Generate deterministic embedding vectors from BLAKE3 hashes.
    ///
    /// This produces consistent vectors for the same input text without
    /// any network access. The vectors are normalized to unit length.
    /// Useful for testing and offline development.
    fn embed_batch_fallback(&self, texts: &[String]) -> Vec<Vec<f32>> {
        texts
            .iter()
            .map(|text| blake3_embedding(text, self.config.dimensions))
            .collect()
    }
}

/// Generate a deterministic embedding vector from text using BLAKE3.
///
/// Produces a normalized vector by hashing the text multiple times with
/// different salts to fill all dimensions.
fn blake3_embedding(text: &str, dimensions: usize) -> Vec<f32> {
    let mut vector = Vec::with_capacity(dimensions);

    // Generate enough hash bytes to fill the vector
    // Each hash gives us 32 bytes = 8 f32 values
    let num_hashes = dimensions.div_ceil(8);

    for i in 0..num_hashes {
        let salt = format!("uc-embed-salt-{}", i);
        let mut hasher = blake3::Hasher::new();
        hasher.update(salt.as_bytes());
        hasher.update(text.as_bytes());
        let hash = hasher.finalize();

        // Convert hash bytes to f32 values in [-1, 1]
        let bytes = hash.as_bytes();
        for chunk in bytes.chunks_exact(4) {
            if vector.len() >= dimensions {
                break;
            }
            // Convert 4 bytes to a u32, then map to [-1, 1]
            let bits = u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
            let value = (bits as f64 / u32::MAX as f64) * 2.0 - 1.0;
            vector.push(value as f32);
        }
    }

    // Truncate to exact dimensions
    vector.truncate(dimensions);

    // Normalize to unit length
    let norm: f32 = vector.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm > 0.0 {
        for v in &mut vector {
            *v /= norm;
        }
    } else {
        // Should not happen with BLAKE3, but handle gracefully
        vector = vec![0.0f32; dimensions];
    }

    vector
}

/// Generate a small random jitter for backoff (0-500ms).
fn rand_jitter_ms() -> u64 {
    // Simple deterministic jitter based on current time
    // (good enough for backoff; no need for a full RNG dependency)
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    nanos as u64 % 500
}

// ── Semantic Indexer ─────────────────────────────────────────────

/// Indexer that takes code chunks, computes embeddings, and upserts to Qdrant.
///
/// Works with the `EmbeddingService` for embedding computation and
/// `LongTermMemory` for Qdrant storage.
pub struct SemanticIndexer {
    embedding_service: Arc<EmbeddingService>,
    /// In-memory fallback store for code embeddings when Qdrant is unavailable.
    fallback: Arc<RwLock<Vec<FallbackCodeEmbedding>>>,
}

/// Fallback code embedding entry for when Qdrant is unavailable.
#[derive(Debug, Clone)]
struct FallbackCodeEmbedding {
    point_id: String,
    vector: Vec<f32>,
    repo_id: String,
    file_path: String,
    start_line: u32,
    end_line: u32,
    language: String,
    symbol_name: Option<String>,
    symbol_kind: Option<String>,
    parent_symbol: Option<String>,
    #[allow(dead_code)]
    chunk_type: String,
    #[allow(dead_code)]
    content_hash: String,
    content: String,
}

impl SemanticIndexer {
    /// Create a new semantic indexer.
    pub fn new(embedding_service: Arc<EmbeddingService>) -> Self {
        Self {
            embedding_service,
            fallback: Arc::new(RwLock::new(Vec::new())),
        }
    }

    /// Index a batch of code chunks: compute embeddings and upsert to Qdrant.
    ///
    /// Returns the number of chunks successfully embedded and stored.
    pub async fn index_chunks(
        &self,
        chunks: &[CodeChunk],
        long_term_memory: &crate::memory::long_term::LongTermMemory,
    ) -> Result<u32, EngineError> {
        if chunks.is_empty() {
            return Ok(0);
        }

        // Batch embed all chunk contents
        let texts: Vec<String> = chunks.iter().map(|c| c.content.clone()).collect();
        let embeddings = self.embedding_service.embed_batch(&texts).await?;

        // Upsert each chunk as a memory entry in Qdrant
        let mut count = 0u32;
        for (chunk, embedding) in chunks.iter().zip(embeddings.iter()) {
            let point_id = chunk.id.clone();

            // Use LongTermMemory's write method by creating a MemoryEntry
            let entry = uc_types::memory::MemoryEntry {
                id: uc_types::memory::MemoryId(point_id.clone()),
                key: uc_types::memory::MemoryKey::Project {
                    project_id: chunk.repo_id.clone(),
                    key: format!(
                        "code_embedding:{}:{}:{}",
                        chunk.file_path, chunk.start_line, chunk.end_line
                    ),
                },
                content: uc_types::memory::MemoryContent::Text(chunk.content.clone()),
                metadata: uc_types::memory::MemoryMetadata {
                    source_agent: "semantic_indexer".to_string(),
                    importance: 0.8, // Code embeddings are high importance
                    tags: vec![
                        "code_embedding".to_string(),
                        chunk.language.clone(),
                        format!("chunk_type:{}", format_chunk_type(&chunk.chunk_type)),
                    ],
                    embedding: Some(embedding.clone()),
                },
                created_at: chrono::Utc::now(),
                updated_at: chrono::Utc::now(),
            };

            match long_term_memory.write(&entry).await {
                Ok(()) => {
                    count += 1;
                }
                Err(e) => {
                    tracing::warn!(
                        "Failed to upsert code embedding for {}:{}: {}",
                        chunk.file_path,
                        chunk.start_line,
                        e
                    );
                    // Store in fallback instead
                    let mut fb = self.fallback.write().await;
                    fb.retain(|f| f.point_id != point_id);
                    fb.push(FallbackCodeEmbedding {
                        point_id,
                        vector: embedding.clone(),
                        repo_id: chunk.repo_id.clone(),
                        file_path: chunk.file_path.clone(),
                        start_line: chunk.start_line,
                        end_line: chunk.end_line,
                        language: chunk.language.clone(),
                        symbol_name: chunk.symbol_name.clone(),
                        symbol_kind: chunk.symbol_kind.clone(),
                        parent_symbol: chunk.parent_symbol.clone(),
                        chunk_type: format_chunk_type(&chunk.chunk_type).to_string(),
                        content_hash: chunk.content_hash.clone(),
                        content: chunk.content.clone(),
                    });
                    count += 1; // Still count as embedded (just in fallback)
                }
            }
        }

        Ok(count)
    }

    /// Search code embeddings by vector similarity.
    ///
    /// Embeds the query text, then searches the Qdrant collection.
    pub async fn search(
        &self,
        query: &str,
        repo_ids: &[String],
        languages: &[String],
        max_results: u32,
        long_term_memory: &crate::memory::long_term::LongTermMemory,
    ) -> Result<Vec<uc_types::search::SearchResultItem>, EngineError> {
        let query_embedding = self.embedding_service.embed_single(query).await?;

        let mut results = Vec::new();

        // Search via LongTermMemory (Qdrant or in-memory fallback)
        {
            let scope = if repo_ids.len() == 1 {
                uc_types::memory::MemorySearchScope::Project {
                    project_id: repo_ids[0].clone(),
                }
            } else {
                uc_types::memory::MemorySearchScope::All
            };

            // Use a lower min_score for code embeddings to account for
            // approximate matching (especially with BLAKE3 fallback embeddings)
            let memory_results = long_term_memory
                .search(query_embedding.clone(), &scope, max_results, 0.3)
                .await?;

            let ltm_items: Vec<uc_types::search::SearchResultItem> = memory_results
                .into_iter()
                .filter_map(|mr| {
                    // Only include code embeddings (not other memory entries)
                    if !mr.entry.metadata.tags.contains(&"code_embedding".to_string()) {
                        return None;
                    }

                    // Parse the key to extract file path and line info
                    let (file_path, start_line, end_line) = parse_embedding_key(&mr.entry.key)?;

                    // Apply language filter
                    if !languages.is_empty() {
                        let lang_match = mr.entry.metadata.tags.iter().any(|tag| {
                            languages.iter().any(|lang| tag == lang)
                        });
                        if !lang_match {
                            return None;
                        }
                    }

                    Some(uc_types::search::SearchResultItem {
                        repo_id: match &mr.entry.key {
                            uc_types::memory::MemoryKey::Project { project_id, .. } => project_id.clone(),
                            _ => return None,
                        },
                        file_path,
                        start_line,
                        end_line,
                        content_snippet: match &mr.entry.content {
                            uc_types::memory::MemoryContent::Text(t) => {
                                // Truncate snippet to 200 chars
                                if t.len() > 200 {
                                    format!("{}...", &t[..200])
                                } else {
                                    t.clone()
                                }
                            }
                            uc_types::memory::MemoryContent::Code { code, .. } => {
                                if code.len() > 200 {
                                    format!("{}...", &code[..200])
                                } else {
                                    code.clone()
                                }
                            }
                            _ => String::new(),
                        },
                        match_type: uc_types::search::SearchMode::Semantic,
                        score: mr.score,
                        symbol_name: None,
                        symbol_kind: None,
                        parent_symbol: None,
                    })
                })
                .collect();

            results.extend(ltm_items);
        }

        // Also search the dedicated fallback store (lower threshold for BLAKE3 embeddings)
        {
            let fallback = self.fallback.read().await;
            let query_norm = vector_norm(&query_embedding);
            let fb_items: Vec<uc_types::search::SearchResultItem> = fallback
                .iter()
                .filter(|f| {
                    // Apply repo filter
                    if !repo_ids.is_empty() && !repo_ids.contains(&f.repo_id) {
                        return false;
                    }
                    // Apply language filter
                    if !languages.is_empty() && !languages.contains(&f.language) {
                        return false;
                    }
                    true
                })
                .filter_map(|f| {
                    let score = cosine_similarity(&query_embedding, &f.vector, query_norm);
                    // Lower threshold for fallback BLAKE3 embeddings
                    if score >= 0.3 {
                        Some((f, score))
                    } else {
                        None
                    }
                })
                .map(|(f, score)| uc_types::search::SearchResultItem {
                    repo_id: f.repo_id.clone(),
                    file_path: f.file_path.clone(),
                    start_line: f.start_line,
                    end_line: f.end_line,
                    content_snippet: if f.content.len() > 200 {
                        format!("{}...", &f.content[..200])
                    } else {
                        f.content.clone()
                    },
                    match_type: uc_types::search::SearchMode::Semantic,
                    score,
                    symbol_name: f.symbol_name.clone(),
                    symbol_kind: f.symbol_kind.clone(),
                    parent_symbol: f.parent_symbol.clone(),
                })
                .collect();

            results.extend(fb_items);
        }

        // Sort by score descending
        results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        results.truncate(max_results as usize);

        Ok(results)
    }

    /// Remove all embeddings for a specific file within a repository.
    pub async fn remove_file(&self, repo_id: &str, file_path: &str) -> Result<(), EngineError> {
        let mut fallback = self.fallback.write().await;
        fallback.retain(|f| !(f.repo_id == repo_id && f.file_path == file_path));
        Ok(())
    }

    /// Remove all embeddings for a repository.
    pub async fn remove_repo(&self, repo_id: &str) -> Result<(), EngineError> {
        let mut fallback = self.fallback.write().await;
        fallback.retain(|f| f.repo_id != repo_id);
        Ok(())
    }

    /// Get the embedding service (for direct access).
    pub fn embedding_service(&self) -> &Arc<EmbeddingService> {
        &self.embedding_service
    }
}

// ── Helper functions ─────────────────────────────────────────────

/// Format a ChunkType for storage.
fn format_chunk_type(ct: &ChunkType) -> &'static str {
    match ct {
        ChunkType::File => "file",
        ChunkType::Symbol => "symbol",
        ChunkType::Block => "block",
    }
}

/// Parse an embedding key to extract file_path, start_line, end_line.
pub fn parse_embedding_key(key: &uc_types::memory::MemoryKey) -> Option<(String, u32, u32)> {
    match key {
        uc_types::memory::MemoryKey::Project { key: inner, .. } => {
            // Format: "code_embedding:{file_path}:{start_line}:{end_line}"
            if !inner.starts_with("code_embedding:") {
                return None;
            }
            let rest = &inner["code_embedding:".len()..];
            // Find the last two colons for line numbers
            let parts: Vec<&str> = rest.rsplitn(3, ':').collect();
            if parts.len() != 3 {
                return None;
            }
            // parts are in reverse order: end_line, start_line, file_path
            let end_line: u32 = parts[0].parse().ok()?;
            let start_line: u32 = parts[1].parse().ok()?;
            let file_path = parts[2].to_string();
            Some((file_path, start_line, end_line))
        }
        _ => None,
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

// ── AST-aware chunking ───────────────────────────────────────────

/// Create code chunks from AST-extracted symbols.
///
/// Takes the AST parse result and creates `CodeChunk` instances at the
/// symbol level (AST-aware chunking). Each function, class, method, etc.
/// becomes one chunk.
pub fn create_chunks_from_ast(
    repo_id: &str,
    file_path: &str,
    language: &str,
    content: &str,
    content_hash: &str,
    symbols: &[crate::indexer::ast::ExtractedSymbol],
) -> Vec<CodeChunk> {
    let mut chunks = Vec::new();

    for symbol in symbols {
        // Skip imports — they are too small to be useful as chunks
        if matches!(symbol.kind, uc_types::search::SymbolKind::Import) {
            continue;
        }

        let chunk_id = {
            let mut hasher = blake3::Hasher::new();
            hasher.update(repo_id.as_bytes());
            hasher.update(file_path.as_bytes());
            hasher.update(symbol.start_line.to_le_bytes().as_slice());
            hasher.finalize().to_hex().to_string()
        };

        // Extract the symbol's content from the source
        let symbol_content = symbol.content.clone();

        chunks.push(CodeChunk {
            id: chunk_id,
            repo_id: repo_id.to_string(),
            file_path: file_path.to_string(),
            start_line: symbol.start_line,
            end_line: symbol.end_line,
            content: symbol_content,
            language: language.to_string(),
            symbol_name: Some(symbol.name.clone()),
            symbol_kind: Some(format!("{:?}", symbol.kind).to_lowercase()),
            parent_symbol: symbol.parent_symbol.clone(),
            chunk_type: ChunkType::Symbol,
            content_hash: content_hash.to_string(),
        });
    }

    // Also create a file-level chunk if the file has content
    if !content.is_empty() && !chunks.is_empty() {
        let file_chunk_id = {
            let mut hasher = blake3::Hasher::new();
            hasher.update(repo_id.as_bytes());
            hasher.update(file_path.as_bytes());
            hasher.update(b"file");
            hasher.finalize().to_hex().to_string()
        };

        chunks.push(CodeChunk {
            id: file_chunk_id,
            repo_id: repo_id.to_string(),
            file_path: file_path.to_string(),
            start_line: 1,
            end_line: content.lines().count() as u32,
            content: content.to_string(),
            language: language.to_string(),
            symbol_name: None,
            symbol_kind: None,
            parent_symbol: None,
            chunk_type: ChunkType::File,
            content_hash: content_hash.to_string(),
        });
    }

    chunks
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_blake3_embedding_deterministic() {
        let text = "fn hello() { println!(\"hi\"); }";
        let v1 = blake3_embedding(text, 1024);
        let v2 = blake3_embedding(text, 1024);
        assert_eq!(v1.len(), 1024);
        assert_eq!(v2.len(), 1024);
        // Same input must produce same output
        for (a, b) in v1.iter().zip(v2.iter()) {
            assert!((a - b).abs() < 1e-6);
        }
    }

    #[test]
    fn test_blake3_embedding_normalized() {
        let text = "struct Config { path: String }";
        let v = blake3_embedding(text, 1024);
        let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!((norm - 1.0).abs() < 0.01, "norm = {}", norm);
    }

    #[test]
    fn test_blake3_embedding_different_inputs() {
        let v1 = blake3_embedding("fn foo() {}", 1024);
        let v2 = blake3_embedding("fn bar() {}", 1024);
        // Different inputs should produce different vectors
        let diff: f32 = v1.iter().zip(v2.iter()).map(|(a, b)| (a - b).powi(2)).sum();
        assert!(diff > 0.01, "vectors should differ, diff = {}", diff);
    }

    #[test]
    fn test_blake3_embedding_dimensions() {
        let v = blake3_embedding("test", 128);
        assert_eq!(v.len(), 128);
        let v = blake3_embedding("test", 2048);
        assert_eq!(v.len(), 2048);
    }

    #[tokio::test]
    async fn test_embedding_service_fallback_single() {
        let service = EmbeddingService::new_fallback();
        let embedding = service.embed_single("fn main() {}").await.unwrap();
        assert_eq!(embedding.len(), 1024);
    }

    #[tokio::test]
    async fn test_embedding_service_fallback_batch() {
        let service = EmbeddingService::new_fallback();
        let texts = vec![
            "fn foo() {}".to_string(),
            "struct Bar {}".to_string(),
            "const X: u32 = 1;".to_string(),
        ];
        let embeddings = service.embed_batch(&texts).await.unwrap();
        assert_eq!(embeddings.len(), 3);
        for emb in &embeddings {
            assert_eq!(emb.len(), 1024);
        }
    }

    #[tokio::test]
    async fn test_embedding_service_caching() {
        let service = EmbeddingService::new_fallback();
        let text = "fn cached() {}";

        // First call computes
        let v1 = service.embed_single(text).await.unwrap();
        // Second call should hit cache
        let v2 = service.embed_single(text).await.unwrap();

        for (a, b) in v1.iter().zip(v2.iter()) {
            assert!((a - b).abs() < 1e-6);
        }
    }

    #[tokio::test]
    async fn test_embedding_service_empty_batch() {
        let service = EmbeddingService::new_fallback();
        let embeddings = service.embed_batch(&[]).await.unwrap();
        assert!(embeddings.is_empty());
    }

    #[test]
    fn test_embedding_service_no_api_key() {
        let service = EmbeddingService::new_fallback();
        assert!(!service.is_api_available());
        assert_eq!(service.model_name(), "voyage-code-3");
        assert_eq!(service.dimensions(), 1024);
        assert_eq!(service.batch_size(), 128);
    }

    #[test]
    fn test_embedding_service_with_api_key() {
        let config = EmbeddingConfig {
            voyage_api_key: Some("test-key".to_string()),
            ..EmbeddingConfig::default()
        };
        let service = EmbeddingService::new(config);
        assert!(service.is_api_available());
    }

    #[test]
    fn test_format_chunk_type() {
        assert_eq!(format_chunk_type(&ChunkType::File), "file");
        assert_eq!(format_chunk_type(&ChunkType::Symbol), "symbol");
        assert_eq!(format_chunk_type(&ChunkType::Block), "block");
    }

    #[test]
    fn test_parse_embedding_key() {
        let key = uc_types::memory::MemoryKey::Project {
            project_id: "repo1".to_string(),
            key: "code_embedding:src/main.rs:10:20".to_string(),
        };
        let result = parse_embedding_key(&key);
        assert!(result.is_some());
        let (file_path, start_line, end_line) = result.unwrap();
        assert_eq!(file_path, "src/main.rs");
        assert_eq!(start_line, 10);
        assert_eq!(end_line, 20);
    }

    #[test]
    fn test_parse_embedding_key_invalid() {
        let key = uc_types::memory::MemoryKey::Project {
            project_id: "repo1".to_string(),
            key: "other_key".to_string(),
        };
        assert!(parse_embedding_key(&key).is_none());

        let key2 = uc_types::memory::MemoryKey::Global {
            key: "code_embedding:x:1:2".to_string(),
        };
        assert!(parse_embedding_key(&key2).is_none());
    }

    #[test]
    fn test_cosine_similarity_semantic() {
        let a = vec![1.0, 0.0, 0.0];
        let b = vec![1.0, 0.0, 0.0];
        let norm = vector_norm(&a);
        let sim = cosine_similarity(&a, &b, norm);
        assert!((sim - 1.0).abs() < 0.001);

        let c = vec![0.0, 1.0, 0.0];
        let sim = cosine_similarity(&a, &c, norm);
        assert!((sim - 0.0).abs() < 0.001);
    }

    #[tokio::test]
    async fn test_semantic_indexer_index_and_search() {
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        let indexer = SemanticIndexer::new(embedding_service);
        let long_term = crate::memory::long_term::LongTermMemory::new_fallback();

        let chunks = vec![
            CodeChunk {
                id: "chunk1".to_string(),
                repo_id: "test-repo".to_string(),
                file_path: "src/main.rs".to_string(),
                start_line: 1,
                end_line: 10,
                content: "fn process_data(data: &str) -> Result<String>".to_string(),
                language: "rust".to_string(),
                symbol_name: Some("process_data".to_string()),
                symbol_kind: Some("function".to_string()),
                parent_symbol: None,
                chunk_type: ChunkType::Symbol,
                content_hash: "abc".to_string(),
            },
        ];

        let count = indexer.index_chunks(&chunks, &long_term).await.unwrap();
        assert_eq!(count, 1);

        // Search with the same text as the chunk content
        // (BLAKE3 fallback produces identical vectors for identical text)
        let results = indexer
            .search(
                "fn process_data(data: &str) -> Result<String>",
                &["test-repo".to_string()],
                &[],
                10,
                &long_term,
            )
            .await
            .unwrap();
        // Should find results with exact text match (cosine sim = 1.0)
        assert!(!results.is_empty());
        assert_eq!(results[0].file_path, "src/main.rs");
    }

    #[tokio::test]
    async fn test_semantic_indexer_remove_repo() {
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        let indexer = SemanticIndexer::new(embedding_service);
        let long_term = crate::memory::long_term::LongTermMemory::new_fallback();

        let chunks = vec![CodeChunk {
            id: "chunk-rm".to_string(),
            repo_id: "rm-repo".to_string(),
            file_path: "lib.rs".to_string(),
            start_line: 1,
            end_line: 5,
            content: "fn helper() {}".to_string(),
            language: "rust".to_string(),
            symbol_name: Some("helper".to_string()),
            symbol_kind: Some("function".to_string()),
            parent_symbol: None,
            chunk_type: ChunkType::Symbol,
            content_hash: "def".to_string(),
        }];

        indexer.index_chunks(&chunks, &long_term).await.unwrap();
        indexer.remove_repo("rm-repo").await.unwrap();

        // After removal, fallback should be empty for that repo
        let fallback = indexer.fallback.read().await;
        assert!(fallback.iter().all(|f| f.repo_id != "rm-repo"));
    }

    #[tokio::test]
    async fn test_semantic_indexer_remove_file() {
        let embedding_service = Arc::new(EmbeddingService::new_fallback());
        let indexer = SemanticIndexer::new(embedding_service);
        let long_term = crate::memory::long_term::LongTermMemory::new_fallback();

        let chunks = vec![
            CodeChunk {
                id: "chunk-f1".to_string(),
                repo_id: "rm-repo".to_string(),
                file_path: "lib.rs".to_string(),
                start_line: 1,
                end_line: 5,
                content: "fn helper() {}".to_string(),
                language: "rust".to_string(),
                symbol_name: Some("helper".to_string()),
                symbol_kind: Some("function".to_string()),
                parent_symbol: None,
                chunk_type: ChunkType::Symbol,
                content_hash: "abc".to_string(),
            },
            CodeChunk {
                id: "chunk-f2".to_string(),
                repo_id: "rm-repo".to_string(),
                file_path: "main.rs".to_string(),
                start_line: 1,
                end_line: 3,
                content: "fn main() {}".to_string(),
                language: "rust".to_string(),
                symbol_name: Some("main".to_string()),
                symbol_kind: Some("function".to_string()),
                parent_symbol: None,
                chunk_type: ChunkType::Symbol,
                content_hash: "def".to_string(),
            },
        ];

        indexer.index_chunks(&chunks, &long_term).await.unwrap();

        // Manually add entries to the local fallback store to verify remove_file works.
        // (index_chunks may store data in LongTermMemory instead of fallback, so we
        // inject fallback entries directly to test the removal logic.)
        {
            let mut fallback = indexer.fallback.write().await;
            fallback.push(FallbackCodeEmbedding {
                point_id: "fb-1".to_string(),
                vector: vec![0.1; 1024],
                repo_id: "rm-repo".to_string(),
                file_path: "lib.rs".to_string(),
                start_line: 1,
                end_line: 5,
                language: "rust".to_string(),
                symbol_name: Some("helper".to_string()),
                symbol_kind: Some("function".to_string()),
                parent_symbol: None,
                chunk_type: "symbol".to_string(),
                content_hash: "abc".to_string(),
                content: "fn helper() {}".to_string(),
            });
            fallback.push(FallbackCodeEmbedding {
                point_id: "fb-2".to_string(),
                vector: vec![0.2; 1024],
                repo_id: "rm-repo".to_string(),
                file_path: "main.rs".to_string(),
                start_line: 1,
                end_line: 3,
                language: "rust".to_string(),
                symbol_name: Some("main".to_string()),
                symbol_kind: Some("function".to_string()),
                parent_symbol: None,
                chunk_type: "symbol".to_string(),
                content_hash: "def".to_string(),
                content: "fn main() {}".to_string(),
            });
        }

        indexer.remove_file("rm-repo", "lib.rs").await.unwrap();

        // After removal, lib.rs should be gone but main.rs should remain
        let fallback = indexer.fallback.read().await;
        assert!(fallback.iter().all(|f| !(f.repo_id == "rm-repo" && f.file_path == "lib.rs")));
        assert!(fallback.iter().any(|f| f.repo_id == "rm-repo" && f.file_path == "main.rs"));
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_create_chunks_from_ast() {
        use crate::indexer::ast::ExtractedSymbol;

        let symbols = vec![
            ExtractedSymbol {
                name: "process_data".to_string(),
                kind: uc_types::search::SymbolKind::Function,
                start_line: 1,
                start_col: 0,
                end_line: 5,
                end_col: 1,
                parent_symbol: None,
                content: "fn process_data() {}".to_string(),
            },
            ExtractedSymbol {
                name: "Config".to_string(),
                kind: uc_types::search::SymbolKind::Struct,
                start_line: 7,
                start_col: 0,
                end_line: 10,
                end_col: 1,
                parent_symbol: None,
                content: "struct Config { path: String }".to_string(),
            },
        ];

        let source = "fn process_data() {}\n\nstruct Config { path: String }\n";
        let chunks = create_chunks_from_ast(
            "test-repo",
            "src/main.rs",
            "rust",
            source,
            "hash123",
            &symbols,
        );

        // Should have 3 chunks: 2 symbols + 1 file-level
        assert_eq!(chunks.len(), 3);

        // First chunk: function symbol
        assert_eq!(chunks[0].symbol_name.as_deref(), Some("process_data"));
        assert_eq!(chunks[0].chunk_type, ChunkType::Symbol);
        assert_eq!(chunks[0].start_line, 1);
        assert_eq!(chunks[0].end_line, 5);

        // Second chunk: struct symbol
        assert_eq!(chunks[1].symbol_name.as_deref(), Some("Config"));
        assert_eq!(chunks[1].chunk_type, ChunkType::Symbol);

        // Third chunk: file-level
        assert_eq!(chunks[2].chunk_type, ChunkType::File);
        assert!(chunks[2].symbol_name.is_none());
    }

    #[cfg(feature = "indexing")]
    #[test]
    fn test_create_chunks_from_ast_skips_imports() {
        use crate::indexer::ast::ExtractedSymbol;

        let symbols = vec![
            ExtractedSymbol {
                name: "std::collections".to_string(),
                kind: uc_types::search::SymbolKind::Import,
                start_line: 1,
                start_col: 0,
                end_line: 1,
                end_col: 30,
                parent_symbol: None,
                content: "use std::collections;".to_string(),
            },
            ExtractedSymbol {
                name: "main".to_string(),
                kind: uc_types::search::SymbolKind::Function,
                start_line: 3,
                start_col: 0,
                end_line: 5,
                end_col: 1,
                parent_symbol: None,
                content: "fn main() {}".to_string(),
            },
        ];

        let source = "use std::collections;\n\nfn main() {}\n";
        let chunks = create_chunks_from_ast("repo", "main.rs", "rust", source, "h", &symbols);

        // Import should be skipped; only function + file-level
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0].symbol_name.as_deref(), Some("main"));
    }
}
