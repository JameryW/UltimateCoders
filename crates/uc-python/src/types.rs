//! Python-facing type wrappers.
//!
//! Thin PyO3 wrappers around uc-types data structures, providing
//! Pythonic APIs for search, memory, and index operations.

use pyo3::prelude::*;

// ── Search types ──────────────────────────────────────────

/// Python SearchQuery wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PySearchQuery {
    pub query: String,
    pub modes: Vec<String>,
    pub repo_ids: Vec<String>,
    pub languages: Vec<String>,
    pub max_results: u32,
}

#[pymethods]
impl PySearchQuery {
    #[new]
    #[pyo3(signature = (query, modes=None, repo_ids=None, languages=None, max_results=10))]
    pub fn new(
        query: String,
        modes: Option<Vec<String>>,
        repo_ids: Option<Vec<String>>,
        languages: Option<Vec<String>>,
        max_results: u32,
    ) -> Self {
        Self {
            query,
            modes: modes.unwrap_or_default(),
            repo_ids: repo_ids.unwrap_or_default(),
            languages: languages.unwrap_or_default(),
            max_results,
        }
    }

    /// The search text.
    #[getter]
    pub fn query(&self) -> &str {
        &self.query
    }
}

impl From<PySearchQuery> for uc_types::SearchQuery {
    fn from(q: PySearchQuery) -> Self {
        Self {
            query: q.query,
            modes: q.modes.iter().map(|m| match m.as_str() {
                "text" => uc_types::SearchMode::Text,
                "semantic" => uc_types::SearchMode::Semantic,
                "ast" => uc_types::SearchMode::Ast,
                _ => uc_types::SearchMode::Hybrid,
            }).collect(),
            repo_ids: q.repo_ids,
            languages: q.languages,
            path_patterns: vec![],
            max_results: q.max_results,
        }
    }
}

/// Python SearchResult wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PySearchResult {
    pub items: Vec<PySearchResultItem>,
}

#[pymethods]
impl PySearchResult {
    /// Number of result items.
    pub fn __len__(&self) -> usize {
        self.items.len()
    }

    /// Get items.
    #[getter]
    pub fn items(&self) -> Vec<PySearchResultItem> {
        self.items.clone()
    }
}

impl From<uc_types::SearchResult> for PySearchResult {
    fn from(r: uc_types::SearchResult) -> Self {
        Self {
            items: r.items.into_iter().map(Into::into).collect(),
        }
    }
}

/// Python SearchResultItem wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PySearchResultItem {
    #[pyo3(get)]
    pub repo_id: String,
    #[pyo3(get)]
    pub file_path: String,
    #[pyo3(get)]
    pub start_line: u32,
    #[pyo3(get)]
    pub end_line: u32,
    #[pyo3(get)]
    pub content_snippet: String,
    #[pyo3(get)]
    pub score: f32,
    #[pyo3(get)]
    pub match_type: String,
    #[pyo3(get)]
    pub symbol_name: Option<String>,
    #[pyo3(get)]
    pub symbol_kind: Option<String>,
    #[pyo3(get)]
    pub parent_symbol: Option<String>,
}

impl From<uc_types::SearchResultItem> for PySearchResultItem {
    fn from(item: uc_types::SearchResultItem) -> Self {
        Self {
            repo_id: item.repo_id,
            file_path: item.file_path,
            start_line: item.start_line,
            end_line: item.end_line,
            content_snippet: item.content_snippet,
            score: item.score,
            match_type: match item.match_type {
                uc_types::SearchMode::Text => "text".to_string(),
                uc_types::SearchMode::Semantic => "semantic".to_string(),
                uc_types::SearchMode::Ast => "ast".to_string(),
                uc_types::SearchMode::Hybrid => "hybrid".to_string(),
            },
            symbol_name: item.symbol_name,
            symbol_kind: item.symbol_kind,
            parent_symbol: item.parent_symbol,
        }
    }
}

// ── Memory types ──────────────────────────────────────────

/// Python MemoryEntry wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PyMemoryEntry {
    #[pyo3(get)]
    pub id: String,
    #[pyo3(get)]
    pub content_type: String,
    #[pyo3(get)]
    pub content: String,
    #[pyo3(get)]
    pub source_agent: String,
    #[pyo3(get)]
    pub importance: f32,
    #[pyo3(get)]
    pub tags: Vec<String>,
    #[pyo3(get)]
    pub created_at: i64,
    #[pyo3(get)]
    pub updated_at: i64,
    #[pyo3(get)]
    pub key_scope: String,
    #[pyo3(get)]
    pub key: String,
    #[pyo3(get)]
    pub task_id: String,
    #[pyo3(get)]
    pub project_id: String,
}

#[pymethods]
impl PyMemoryEntry {
    fn __repr__(&self) -> String {
        format!(
            "MemoryEntry(id={}, key={}:{}:{}, content_type={})",
            self.id, self.key_scope, self.task_id, self.key, self.content_type
        )
    }
}

impl From<uc_types::MemoryEntry> for PyMemoryEntry {
    fn from(entry: uc_types::MemoryEntry) -> Self {
        let (content_type, content) = match &entry.content {
            uc_types::MemoryContent::Text(s) => ("text".to_string(), s.clone()),
            uc_types::MemoryContent::Structured(v) => ("structured".to_string(), v.to_string()),
            uc_types::MemoryContent::Code { language, code } => {
                ("code".to_string(), format!("{}:{}", language, code))
            }
            uc_types::MemoryContent::Diff { file_path, diff } => {
                ("diff".to_string(), format!("{}:{}", file_path, diff))
            }
            uc_types::MemoryContent::Reference { uri, description } => {
                ("reference".to_string(), format!("{}:{}", uri, description))
            }
        };
        let (key_scope, task_id, project_id, key) = match &entry.key {
            uc_types::MemoryKey::Task { task_id, key } => {
                ("task".to_string(), task_id.clone(), String::new(), key.clone())
            }
            uc_types::MemoryKey::Project { project_id, key } => {
                ("project".to_string(), String::new(), project_id.clone(), key.clone())
            }
            uc_types::MemoryKey::Global { key } => {
                ("global".to_string(), String::new(), String::new(), key.clone())
            }
        };
        Self {
            id: entry.id.0,
            content_type,
            content,
            source_agent: entry.metadata.source_agent,
            importance: entry.metadata.importance,
            tags: entry.metadata.tags,
            created_at: entry.created_at.timestamp(),
            updated_at: entry.updated_at.timestamp(),
            key_scope,
            key,
            task_id,
            project_id,
        }
    }
}

/// Python MemorySearchResult wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PyMemorySearchResult {
    #[pyo3(get)]
    pub entry: PyMemoryEntry,
    #[pyo3(get)]
    pub score: f32,
}

impl From<uc_types::MemorySearchResult> for PyMemorySearchResult {
    fn from(r: uc_types::MemorySearchResult) -> Self {
        Self {
            entry: r.entry.into(),
            score: r.score,
        }
    }
}

// ── Index types ───────────────────────────────────────────

/// Python IndexResponse wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PyIndexResponse {
    #[pyo3(get)]
    pub repo_id: String,
    #[pyo3(get)]
    pub files_indexed: u32,
    #[pyo3(get)]
    pub symbols_extracted: u32,
    #[pyo3(get)]
    pub chunks_embedded: u32,
    #[pyo3(get)]
    pub duration_ms: u64,
}

impl From<uc_types::IndexResponse> for PyIndexResponse {
    fn from(r: uc_types::IndexResponse) -> Self {
        Self {
            repo_id: r.repo_id,
            files_indexed: r.files_indexed,
            symbols_extracted: r.symbols_extracted,
            chunks_embedded: r.chunks_embedded,
            duration_ms: r.duration_ms,
        }
    }
}

/// Python RepoIndexState wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PyRepoIndexState {
    #[pyo3(get)]
    pub repo_id: String,
    #[pyo3(get)]
    pub indexed: bool,
    #[pyo3(get)]
    pub last_indexed_sha: Option<String>,
    #[pyo3(get)]
    pub files_count: u32,
    #[pyo3(get)]
    pub symbols_count: u32,
    #[pyo3(get)]
    pub chunks_count: u32,
}

impl From<uc_types::RepoIndexState> for PyRepoIndexState {
    fn from(s: uc_types::RepoIndexState) -> Self {
        Self {
            repo_id: s.repo_id,
            indexed: s.indexed,
            last_indexed_sha: s.last_indexed_sha,
            files_count: s.files_count,
            symbols_count: s.symbols_count,
            chunks_count: s.chunks_count,
        }
    }
}
