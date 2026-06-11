//! PyEngine — Python-facing Engine class.
//!
//! Wraps either LocalEngine or GrpcEngineClient behind a unified interface.
//! Uses pyo3-async-runtimes for async methods and py.allow_threads() for
//! sync wrappers that block on the tokio runtime.

use pyo3::prelude::*;

use uc_types::EngineApi;

use crate::async_support;
use crate::types::*;

/// Unified engine interface. Switches between local (PyO3) and remote (gRPC).
///
/// Usage in Python:
///   engine = PyEngine(mode="local")
///   engine = PyEngine(mode="grpc", grpc_endpoint="http://localhost:50051")
#[pyclass]
pub struct PyEngine {
    mode: String,
    inner: Box<dyn EngineApi + Send + Sync>,
}

/// Convert EngineError to a Python exception.
fn engine_error_to_pyerr(err: uc_types::EngineError) -> PyErr {
    use uc_types::EngineError::*;
    match err {
        SearchError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        IndexError(msg) => pyo3::exceptions::PyKeyError::new_err(msg),
        MemoryReadError(msg) | MemoryWriteError(msg) => {
            pyo3::exceptions::PyRuntimeError::new_err(msg)
        }
        IndexingError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        ConnectionError(msg) => pyo3::exceptions::PyConnectionError::new_err(msg),
        TimeoutError(msg) => pyo3::exceptions::PyTimeoutError::new_err(msg),
        RateLimited(secs) => {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Rate limited, retry after {}s", secs))
        }
        ConflictError { path, details } => {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Conflict in {}: {}", path, details))
        }
        TaskError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        WorkerUnavailable(msg) => pyo3::exceptions::PyConnectionError::new_err(msg),
        SandboxError(msg) => pyo3::exceptions::PyPermissionError::new_err(msg),
        ConfigError(msg) => pyo3::exceptions::PyValueError::new_err(msg),
        InternalError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
    }
}

#[pymethods]
impl PyEngine {
    /// Create a new engine instance.
    ///
    /// Args:
    ///     mode: "local" (in-process) or "grpc" (remote server)
    ///     grpc_endpoint: Required if mode="grpc"
    #[new]
    #[pyo3(signature = (mode="local", grpc_endpoint=None))]
    pub fn new(mode: &str, grpc_endpoint: Option<&str>) -> PyResult<Self> {
        match mode {
            "local" => {
                let engine = uc_engine::LocalEngine::new_fallback();
                let inner: Box<dyn EngineApi + Send + Sync> = Box::new(engine);
                Ok(PyEngine {
                    mode: mode.to_string(),
                    inner,
                })
            }
            "grpc" => {
                let endpoint = grpc_endpoint.ok_or_else(|| {
                    pyo3::exceptions::PyValueError::new_err(
                        "grpc_endpoint is required when mode='grpc'",
                    )
                })?;
                // For gRPC mode, we need to connect asynchronously.
                // We'll block the current thread to do so (acceptable at construction time).
                let client = async_support::block_on(
                    uc_grpc::client::GrpcEngineClient::connect(endpoint),
                )
                .map_err(engine_error_to_pyerr)?;
                let inner: Box<dyn EngineApi + Send + Sync> = Box::new(client);
                Ok(PyEngine {
                    mode: mode.to_string(),
                    inner,
                })
            }
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "mode must be 'local' or 'grpc', got '{}'",
                mode
            ))),
        }
    }

    /// Check engine health. Returns full health status object.
    pub fn health(&self, py: Python<'_>) -> PyResult<PyHealthStatus> {
        let inner = &self.inner;
        let result = py.allow_threads(|| {
            async_support::block_on(inner.health())
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Search across indexed repositories.
    ///
    /// Args:
    ///     query: SearchQuery object
    pub fn search(&self, py: Python<'_>, query: PySearchQuery) -> PyResult<PySearchResult> {
        let inner = &self.inner;
        let uc_query: uc_types::SearchQuery = query.into();
        let result = py.allow_threads(|| {
            async_support::block_on(inner.search(uc_query))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Index a repository.
    ///
    /// Args:
    ///     repo_id: Repository identifier
    ///     local_path: Local path to the repository
    ///     remote_url: Git remote URL (optional)
    ///     default_branch: Default branch name (default: "main")
    ///     force_full: Force full reindex (default: False)
    #[pyo3(signature = (repo_id, local_path, remote_url=None, default_branch="main".to_string(), force_full=false))]
    pub fn index_repo(
        &self,
        py: Python<'_>,
        repo_id: String,
        local_path: String,
        remote_url: Option<String>,
        default_branch: String,
        force_full: bool,
    ) -> PyResult<PyIndexResponse> {
        let inner = &self.inner;
        let request = uc_types::IndexRequest {
            repo: uc_types::RepoSpec {
                repo_id,
                remote_url: remote_url.unwrap_or_default(),
                default_branch,
                local_path: Some(local_path),
            },
            force_full,
        };
        let result = py.allow_threads(|| {
            async_support::block_on(inner.index_repo(request))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Get the current index state for a repository.
    pub fn get_index_state(&self, py: Python<'_>, repo_id: String) -> PyResult<PyRepoIndexState> {
        let inner = &self.inner;
        let result = py.allow_threads(|| {
            async_support::block_on(inner.get_index_state(&repo_id))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Get detailed index state for a repository (includes health and version info).
    pub fn get_detailed_index_state(&self, py: Python<'_>, repo_id: String) -> PyResult<PyIndexState> {
        let inner = &self.inner;
        // get_index_state returns RepoIndexState; we convert to IndexState equivalent
        // by delegating through the engine. Since EngineApi only provides get_index_state,
        // we use that and enhance it with fallback defaults for the detailed fields.
        let result = py.allow_threads(|| {
            async_support::block_on(inner.get_index_state(&repo_id))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(PyIndexState::from_repo_index_state(result))
    }

    /// Remove a repository's index.
    pub fn remove_index(&self, py: Python<'_>, repo_id: String) -> PyResult<()> {
        let inner = &self.inner;
        py.allow_threads(|| {
            async_support::block_on(inner.remove_index(&repo_id))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(())
    }

    /// Read a memory entry.
    ///
    /// Args:
    ///     key_scope: "task", "project", or "global"
    ///     key: The memory key
    ///     task_id: Task ID (required if key_scope="task")
    ///     project_id: Project ID (required if key_scope="project")
    ///     include_semantic: Also search long-term memory semantically
    #[pyo3(signature = (key_scope, key, task_id=None, project_id=None, include_semantic=false))]
    pub fn read_memory(
        &self,
        py: Python<'_>,
        key_scope: String,
        key: String,
        task_id: Option<String>,
        project_id: Option<String>,
        include_semantic: bool,
    ) -> PyResult<Option<PyMemoryEntry>> {
        let inner = &self.inner;
        let mem_key = match key_scope.as_str() {
            "task" => uc_types::MemoryKey::Task {
                task_id: task_id.unwrap_or_default(),
                key,
            },
            "project" => uc_types::MemoryKey::Project {
                project_id: project_id.unwrap_or_default(),
                key,
            },
            "global" => uc_types::MemoryKey::Global { key },
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "key_scope must be 'task', 'project', or 'global'",
                ))
            }
        };
        let request = uc_types::MemoryReadRequest {
            key: mem_key,
            include_semantic,
        };
        let result = py.allow_threads(|| {
            async_support::block_on(inner.read_memory(request))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.map(Into::into))
    }

    /// Write a memory entry.
    ///
    /// Args:
    ///     key_scope: "task", "project", or "global"
    ///     key: The memory key
    ///     content: The content to store
    ///     content_type: "text" (default), "structured", "code", "diff", "reference"
    ///     source_agent: Agent that created this memory (default: "python")
    ///     importance: Importance score 0.0-1.0 (default: 0.5)
    ///     tags: Tags for categorization
    ///     task_id: Task ID (required if key_scope="task")
    ///     project_id: Project ID (required if key_scope="project")
    ///     language: Language for content_type="code"
    ///     file_path: File path for content_type="diff"
    ///     uri: URI for content_type="reference"
    ///     description: Description for content_type="reference"
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (key_scope, key, content, content_type="text".to_string(), source_agent="python".to_string(), importance=0.5, tags=None, task_id=None, project_id=None, language=None, file_path=None, uri=None, description=None))]
    pub fn write_memory(
        &self,
        py: Python<'_>,
        key_scope: String,
        key: String,
        content: String,
        content_type: String,
        source_agent: String,
        importance: f32,
        tags: Option<Vec<String>>,
        task_id: Option<String>,
        project_id: Option<String>,
        language: Option<String>,
        file_path: Option<String>,
        uri: Option<String>,
        description: Option<String>,
    ) -> PyResult<PyMemoryEntry> {
        let inner = &self.inner;
        let mem_key = match key_scope.as_str() {
            "task" => uc_types::MemoryKey::Task {
                task_id: task_id.unwrap_or_default(),
                key,
            },
            "project" => uc_types::MemoryKey::Project {
                project_id: project_id.unwrap_or_default(),
                key,
            },
            "global" => uc_types::MemoryKey::Global { key },
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "key_scope must be 'task', 'project', or 'global'",
                ))
            }
        };
        let mem_content = match content_type.as_str() {
            "structured" => uc_types::MemoryContent::Structured(
                serde_json::from_str(&content).unwrap_or(serde_json::Value::String(content)),
            ),
            "code" => uc_types::MemoryContent::Code {
                language: language.unwrap_or_default(),
                code: content,
            },
            "diff" => uc_types::MemoryContent::Diff {
                file_path: file_path.unwrap_or_default(),
                diff: content,
            },
            "reference" => uc_types::MemoryContent::Reference {
                uri: uri.unwrap_or_default(),
                description: description.unwrap_or_default(),
            },
            _ => uc_types::MemoryContent::Text(content),
        };
        let request = uc_types::MemoryWriteRequest {
            key: mem_key,
            content: mem_content,
            metadata: uc_types::MemoryMetadata {
                source_agent,
                importance,
                tags: tags.unwrap_or_default(),
                embedding: None,
            },
        };
        let result = py.allow_threads(|| {
            async_support::block_on(inner.write_memory(request))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Delete a memory entry.
    ///
    /// Args:
    ///     key_scope: "task", "project", or "global"
    ///     key: The memory key
    ///     task_id: Task ID (required if key_scope="task")
    ///     project_id: Project ID (required if key_scope="project")
    #[pyo3(signature = (key_scope, key, task_id=None, project_id=None))]
    pub fn delete_memory(
        &self,
        py: Python<'_>,
        key_scope: String,
        key: String,
        task_id: Option<String>,
        project_id: Option<String>,
    ) -> PyResult<()> {
        let inner = &self.inner;
        let mem_key = match key_scope.as_str() {
            "task" => uc_types::MemoryKey::Task {
                task_id: task_id.unwrap_or_default(),
                key,
            },
            "project" => uc_types::MemoryKey::Project {
                project_id: project_id.unwrap_or_default(),
                key,
            },
            "global" => uc_types::MemoryKey::Global { key },
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "key_scope must be 'task', 'project', or 'global'",
                ))
            }
        };
        py.allow_threads(|| {
            async_support::block_on(inner.delete_memory(&mem_key))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(())
    }

    /// Search long-term memory semantically.
    ///
    /// Args:
    ///     query: Search query text
    ///     scope_type: "project", "global", or "all" (default: "all")
    ///     project_id: Project ID (required if scope_type="project")
    ///     max_results: Maximum number of results (default: 20)
    ///     min_score: Minimum similarity score 0.0-1.0 (default: 0.5)
    #[pyo3(signature = (query, scope_type="all".to_string(), project_id=None, max_results=20, min_score=0.5))]
    pub fn search_memory(
        &self,
        py: Python<'_>,
        query: String,
        scope_type: String,
        project_id: Option<String>,
        max_results: u32,
        min_score: f32,
    ) -> PyResult<Vec<PyMemorySearchResult>> {
        let inner = &self.inner;
        let scope = match scope_type.as_str() {
            "project" => uc_types::MemorySearchScope::Project {
                project_id: project_id.unwrap_or_default(),
            },
            "global" => uc_types::MemorySearchScope::Global,
            _ => uc_types::MemorySearchScope::All,
        };
        let request = uc_types::MemorySearchRequest {
            query,
            scope,
            max_results,
            min_score,
        };
        let result = py.allow_threads(|| {
            async_support::block_on(inner.search_memory(request))
        })
        .map_err(engine_error_to_pyerr)?;
        Ok(result.results.into_iter().map(Into::into).collect())
    }

    /// Get the current mode.
    #[getter]
    pub fn mode(&self) -> &str {
        &self.mode
    }
}
