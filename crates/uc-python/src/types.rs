//! Python-facing type wrappers.
//!
//! Thin PyO3 wrappers around uc-types data structures, providing
//! Pythonic APIs for search, memory, and index operations.

use pyo3::prelude::*;

// ── Health types ──────────────────────────────────────────

/// Full health status returned by the engine.
#[pyclass]
#[derive(Clone)]
pub struct PyHealthStatus {
    #[pyo3(get)]
    pub status: String,
    #[pyo3(get)]
    pub version: String,
    #[pyo3(get)]
    pub uptime_seconds: u64,
    #[pyo3(get)]
    pub components: Vec<PyComponentHealth>,
}

#[pymethods]
impl PyHealthStatus {
    fn __repr__(&self) -> String {
        format!(
            "HealthStatus(status={}, version={}, uptime={}s, components={})",
            self.status,
            self.version,
            self.uptime_seconds,
            self.components.len()
        )
    }
}

impl From<uc_types::HealthStatus> for PyHealthStatus {
    fn from(h: uc_types::HealthStatus) -> Self {
        Self {
            status: h.status,
            version: h.version,
            uptime_seconds: h.uptime_seconds,
            components: h.components.into_iter().map(Into::into).collect(),
        }
    }
}

/// Health of an individual component.
#[pyclass]
#[derive(Clone)]
pub struct PyComponentHealth {
    #[pyo3(get)]
    pub name: String,
    #[pyo3(get)]
    pub status: String,
    #[pyo3(get)]
    pub details: Option<String>,
}

#[pymethods]
impl PyComponentHealth {
    fn __repr__(&self) -> String {
        match &self.details {
            Some(d) => format!(
                "ComponentHealth(name={}, status={}, details={})",
                self.name, self.status, d
            ),
            None => format!(
                "ComponentHealth(name={}, status={})",
                self.name, self.status
            ),
        }
    }
}

impl From<uc_types::ComponentHealth> for PyComponentHealth {
    fn from(c: uc_types::ComponentHealth) -> Self {
        Self {
            name: c.name,
            status: c.status,
            details: c.details,
        }
    }
}

// ── Search types ──────────────────────────────────────────

/// Python SearchQuery wrapper.
#[pyclass]
#[derive(Clone)]
pub struct PySearchQuery {
    pub query: String,
    pub modes: Vec<String>,
    pub repo_ids: Vec<String>,
    pub languages: Vec<String>,
    pub path_patterns: Vec<String>,
    pub max_results: u32,
}

#[pymethods]
impl PySearchQuery {
    #[new]
    #[pyo3(signature = (query, modes=None, repo_ids=None, languages=None, path_patterns=None, max_results=10))]
    pub fn new(
        query: String,
        modes: Option<Vec<String>>,
        repo_ids: Option<Vec<String>>,
        languages: Option<Vec<String>>,
        path_patterns: Option<Vec<String>>,
        max_results: u32,
    ) -> Self {
        Self {
            query,
            modes: modes.unwrap_or_default(),
            repo_ids: repo_ids.unwrap_or_default(),
            languages: languages.unwrap_or_default(),
            path_patterns: path_patterns.unwrap_or_default(),
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
            modes: q
                .modes
                .iter()
                .map(|m| match m.as_str() {
                    "text" => uc_types::SearchMode::Text,
                    "semantic" => uc_types::SearchMode::Semantic,
                    "ast" => uc_types::SearchMode::Ast,
                    _ => uc_types::SearchMode::Hybrid,
                })
                .collect(),
            repo_ids: q.repo_ids,
            languages: q.languages,
            path_patterns: q.path_patterns,
            max_results: q.max_results,
        }
    }
}

/// Python SearchResult wrapper — iterable container of result items.
#[pyclass]
#[derive(Clone)]
pub struct PySearchResult {
    pub items: Vec<PySearchResultItem>,
}

/// Iterator for PySearchResult.
#[pyclass]
pub struct PyResultIterator {
    items: Vec<PySearchResultItem>,
    index: usize,
}

#[pymethods]
impl PyResultIterator {
    fn __next__(mut slf: PyRefMut<'_, Self>) -> Option<PySearchResultItem> {
        if slf.index < slf.items.len() {
            let item = slf.items[slf.index].clone();
            slf.index += 1;
            Some(item)
        } else {
            None
        }
    }
}

#[pymethods]
impl PySearchResult {
    /// Number of result items.
    pub fn __len__(&self) -> usize {
        self.items.len()
    }

    /// Iterate over result items.
    fn __iter__(slf: PyRef<'_, Self>) -> PyResult<PyResultIterator> {
        Ok(PyResultIterator {
            items: slf.items.clone(),
            index: 0,
        })
    }

    /// Get item by index (supports negative indexing).
    fn __getitem__(&self, idx: isize) -> PyResult<PySearchResultItem> {
        let len = self.items.len() as isize;
        let resolved = if idx < 0 { len + idx } else { idx };
        if resolved < 0 || resolved >= len {
            Err(pyo3::exceptions::PyIndexError::new_err(format!(
                "index {} out of range for SearchResult of length {}",
                idx, len
            )))
        } else {
            Ok(self.items[resolved as usize].clone())
        }
    }

    fn __repr__(&self) -> String {
        format!("SearchResult(count={})", self.items.len())
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

/// AST query type for structured code queries.
#[pyclass]
#[derive(Clone)]
pub struct PyAstQuery {
    #[pyo3(get)]
    pub query_type: String,
    #[pyo3(get)]
    pub symbol_name: Option<String>,
    #[pyo3(get)]
    pub symbol_kind: Option<String>,
    #[pyo3(get)]
    pub repo_id: Option<String>,
}

#[pymethods]
impl PyAstQuery {
    #[new]
    #[pyo3(signature = (query_type, symbol_name=None, symbol_kind=None, repo_id=None))]
    pub fn new(
        query_type: String,
        symbol_name: Option<String>,
        symbol_kind: Option<String>,
        repo_id: Option<String>,
    ) -> Self {
        Self {
            query_type,
            symbol_name,
            symbol_kind,
            repo_id,
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "AstQuery(type={}, symbol={}, kind={})",
            self.query_type,
            self.symbol_name.as_deref().unwrap_or("None"),
            self.symbol_kind.as_deref().unwrap_or("None"),
        )
    }
}

// ── Memory types ──────────────────────────────────────────

/// Python MemoryEntry wrapper with structured content fields.
#[pyclass]
#[derive(Clone)]
pub struct PyMemoryEntry {
    #[pyo3(get)]
    pub id: String,
    #[pyo3(get)]
    pub key_scope: String,
    #[pyo3(get)]
    pub key: String,
    #[pyo3(get)]
    pub task_id: Option<String>,
    #[pyo3(get)]
    pub project_id: Option<String>,
    #[pyo3(get)]
    pub content_type: String,
    #[pyo3(get)]
    pub content: String,
    #[pyo3(get)]
    pub language: Option<String>,
    #[pyo3(get)]
    pub file_path: Option<String>,
    #[pyo3(get)]
    pub uri: Option<String>,
    #[pyo3(get)]
    pub description: Option<String>,
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
}

#[pymethods]
impl PyMemoryEntry {
    fn __repr__(&self) -> String {
        format!(
            "MemoryEntry(id={}, key={}:{}:{}, content_type={})",
            self.id,
            self.key_scope,
            match (&self.task_id, &self.project_id) {
                (Some(t), _) => t.as_str(),
                (_, Some(p)) => p.as_str(),
                _ => "",
            },
            self.key,
            self.content_type
        )
    }
}

impl From<uc_types::MemoryEntry> for PyMemoryEntry {
    fn from(entry: uc_types::MemoryEntry) -> Self {
        let (content_type, content, language, file_path, uri, description) = match &entry.content {
            uc_types::MemoryContent::Text(s) => {
                ("text".to_string(), s.clone(), None, None, None, None)
            }
            uc_types::MemoryContent::Structured(v) => (
                "structured".to_string(),
                v.to_string(),
                None,
                None,
                None,
                None,
            ),
            uc_types::MemoryContent::Code {
                language: lang,
                code,
            } => (
                "code".to_string(),
                code.clone(),
                Some(lang.clone()),
                None,
                None,
                None,
            ),
            uc_types::MemoryContent::Diff {
                file_path: fp,
                diff,
            } => (
                "diff".to_string(),
                diff.clone(),
                None,
                Some(fp.clone()),
                None,
                None,
            ),
            uc_types::MemoryContent::Reference {
                uri: u,
                description: d,
            } => (
                "reference".to_string(),
                String::new(),
                None,
                None,
                Some(u.clone()),
                Some(d.clone()),
            ),
        };
        let (key_scope, task_id, project_id, key) = match &entry.key {
            uc_types::MemoryKey::Task { task_id, key } => {
                ("task".to_string(), Some(task_id.clone()), None, key.clone())
            }
            uc_types::MemoryKey::Project { project_id, key } => (
                "project".to_string(),
                None,
                Some(project_id.clone()),
                key.clone(),
            ),
            uc_types::MemoryKey::Global { key } => ("global".to_string(), None, None, key.clone()),
        };
        Self {
            id: entry.id.0,
            key_scope,
            key,
            task_id,
            project_id,
            content_type,
            content,
            language,
            file_path,
            uri,
            description,
            source_agent: entry.metadata.source_agent,
            importance: entry.metadata.importance,
            tags: entry.metadata.tags,
            created_at: entry.created_at.timestamp(),
            updated_at: entry.updated_at.timestamp(),
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

/// Detailed index state with health and version info.
#[pyclass]
#[derive(Clone)]
pub struct PyIndexState {
    #[pyo3(get)]
    pub repo_id: String,
    #[pyo3(get)]
    pub last_indexed_sha: String,
    #[pyo3(get)]
    pub last_indexed_at: i64,
    #[pyo3(get)]
    pub last_full_reindex: i64,
    #[pyo3(get)]
    pub index_version: u32,
    #[pyo3(get)]
    pub health: String,
}

#[pymethods]
impl PyIndexState {
    fn __repr__(&self) -> String {
        format!(
            "IndexState(repo_id={}, health={}, version={})",
            self.repo_id, self.health, self.index_version
        )
    }
}

impl PyIndexState {
    /// Create from a RepoIndexState, using fallback defaults for fields
    /// not available in the simpler type.
    pub fn from_repo_index_state(s: uc_types::RepoIndexState) -> Self {
        Self {
            repo_id: s.repo_id,
            last_indexed_sha: s.last_indexed_sha.unwrap_or_default(),
            last_indexed_at: 0,
            last_full_reindex: 0,
            index_version: 0,
            health: if s.indexed {
                "healthy".to_string()
            } else {
                "stale".to_string()
            },
        }
    }
}

impl From<uc_types::IndexState> for PyIndexState {
    fn from(s: uc_types::IndexState) -> Self {
        Self {
            repo_id: s.repo_id,
            last_indexed_sha: s.last_indexed_sha,
            last_indexed_at: s.last_indexed_at.timestamp(),
            last_full_reindex: s.last_full_reindex.timestamp(),
            index_version: s.index_version,
            health: match s.health {
                uc_types::IndexHealth::Healthy => "healthy".to_string(),
                uc_types::IndexHealth::Indexing => "indexing".to_string(),
                uc_types::IndexHealth::Stale => "stale".to_string(),
                uc_types::IndexHealth::Corrupted => "corrupted".to_string(),
            },
        }
    }
}
