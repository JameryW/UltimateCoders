//! PyEngine — Python-facing Engine class.
//!
//! Wraps either LocalEngine or GrpcEngineClient behind a unified interface.
//! Uses pyo3-async-runtimes for async methods and py.allow_threads() for
//! sync wrappers that block on the tokio runtime.

use std::pin::Pin;
use std::sync::Arc;

use futures::Stream;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3_async_runtimes::tokio::future_into_py;

use uc_types::EngineApi;

use crate::async_support;
use crate::types::*;

/// Unified engine interface. Switches between local (PyO3) and remote (gRPC).
///
/// Usage in Python:
///   engine = PyEngine(mode="local")
///   engine = PyEngine(mode="grpc", grpc_endpoint="http://localhost:50051")
///
/// The `grpc_client` field is a ponytail reference to the concrete GrpcEngineClient,
/// needed for non-trait methods like `watch_task` which return streams.
/// Only set when mode="grpc"; None in local mode.
#[pyclass]
pub struct PyEngine {
    mode: String,
    inner: Arc<dyn EngineApi + Send + Sync>,
    /// Direct reference to the GrpcEngineClient for non-trait methods (watch_task).
    /// None in local mode.
    grpc_client: Option<Arc<uc_grpc::client::GrpcEngineClient>>,
}

/// Convert EngineError to a Python exception.
fn engine_error_to_pyerr(err: uc_types::EngineError) -> PyErr {
    use uc_types::EngineError::*;
    match err {
        NotFound(msg) => pyo3::exceptions::PyKeyError::new_err(msg),
        SearchError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        IndexError(msg) => pyo3::exceptions::PyKeyError::new_err(msg),
        MemoryReadError(msg) | MemoryWriteError(msg) => {
            pyo3::exceptions::PyRuntimeError::new_err(msg)
        }
        IndexingError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        ConnectionError(msg) => pyo3::exceptions::PyConnectionError::new_err(msg),
        TimeoutError(msg) => pyo3::exceptions::PyTimeoutError::new_err(msg),
        RateLimited(secs) => pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Rate limited, retry after {}s",
            secs
        )),
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

/// Build a MemoryKey from scope parameters.
///
/// Shared by both sync and async read/write/delete methods.
fn build_memory_key(
    key_scope: &str,
    key: String,
    task_id: Option<String>,
    project_id: Option<String>,
) -> PyResult<uc_types::MemoryKey> {
    match key_scope {
        "task" => Ok(uc_types::MemoryKey::Task {
            task_id: task_id.unwrap_or_default(),
            key,
        }),
        "project" => Ok(uc_types::MemoryKey::Project {
            project_id: project_id.unwrap_or_default(),
            key,
        }),
        "global" => Ok(uc_types::MemoryKey::Global { key }),
        _ => Err(pyo3::exceptions::PyValueError::new_err(
            "key_scope must be 'task', 'project', or 'global'",
        )),
    }
}

/// Build MemoryContent from content type and value parameters.
///
/// Shared by both sync and async write methods.
fn build_memory_content(
    content_type: &str,
    content: String,
    language: Option<String>,
    file_path: Option<String>,
    uri: Option<String>,
    description: Option<String>,
) -> uc_types::MemoryContent {
    match content_type {
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
    }
}

/// Collect events from a stream with timeout and max count.
///
/// Reads up to `max_events` items from the stream within `timeout_secs`.
/// Returns dicts with event_type, task_id, event_id, timestamp fields.
/// On timeout, returns whatever was collected so far (may be empty).
// ponytail: returns Vec<Py<PyDict>> instead of Vec<PyAgentEvent> (type not yet defined)
async fn collect_events(
    stream: Pin<Box<dyn Stream<Item = uc_types::AgentEvent> + Send>>,
    max_events: usize,
    timeout_secs: f64,
) -> PyResult<Vec<Py<PyDict>>> {
    use futures::StreamExt;
    use tokio::time::{timeout, Duration};

    let duration = Duration::from_secs_f64(timeout_secs.max(0.1));
    let result = timeout(duration, async {
        let mut collected = Vec::with_capacity(max_events.min(64));
        let mut stream = std::pin::pin!(stream);
        while collected.len() < max_events {
            match stream.next().await {
                Some(event) => {
                    let dict = Python::with_gil(|py| {
                        let d = PyDict::new(py);
                        let _ = d.set_item("event_id", event.event_id);
                        let _ = d.set_item("timestamp", event.timestamp.to_rfc3339());
                        let (event_type, task_id) = match &event.payload {
                            uc_types::AgentEventPayload::TaskCreated { task } => {
                                ("task_created", task.id.0.clone())
                            }
                            uc_types::AgentEventPayload::SubtaskAssigned { subtask_id, .. } => {
                                ("subtask_assigned", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::WorkerStarted { subtask_id, .. } => {
                                ("worker_started", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::ToolInvoked { subtask_id, .. } => {
                                ("tool_invoked", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::ToolResult { subtask_id, .. } => {
                                ("tool_result", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::FileModified { subtask_id, .. } => {
                                ("file_modified", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::SubtaskCompleted { result, .. } => {
                                ("subtask_completed", result.subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::SubtaskFailed { subtask_id, .. } => {
                                ("subtask_failed", subtask_id.0.clone())
                            }
                            uc_types::AgentEventPayload::CheckpointCreated { task_id, .. } => {
                                ("checkpoint_created", task_id.0.clone())
                            }
                            uc_types::AgentEventPayload::EditIntent { .. } => {
                                ("edit_intent", String::new())
                            }
                            uc_types::AgentEventPayload::ConflictDetected { .. } => {
                                ("conflict_detected", String::new())
                            }
                        };
                        let _ = d.set_item("event_type", event_type);
                        let _ = d.set_item("task_id", task_id);
                        d.into()
                    });
                    collected.push(dict);
                }
                None => break, // Stream ended
            }
        }
        collected
    })
    .await;

    match result {
        Ok(events) => Ok(events),
        Err(_) => Ok(vec![]), // Timeout -- return what we have (may be empty)
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
                let inner: Arc<dyn EngineApi + Send + Sync> = Arc::new(engine);
                Ok(PyEngine {
                    mode: mode.to_string(),
                    inner,
                    grpc_client: None,
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
                let client =
                    async_support::block_on(uc_grpc::client::GrpcEngineClient::connect(endpoint))
                        .map_err(engine_error_to_pyerr)?;
                // Store both a trait-object ref (for EngineApi methods) and a concrete
                // ref (for non-trait methods like watch_task).
                let client_arc: Arc<uc_grpc::client::GrpcEngineClient> = Arc::new(client);
                let grpc_client = Some(client_arc.clone());
                let inner: Arc<dyn EngineApi + Send + Sync> = client_arc;
                Ok(PyEngine {
                    mode: mode.to_string(),
                    inner,
                    grpc_client,
                })
            }
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "mode must be 'local' or 'grpc', got '{}'",
                mode
            ))),
        }
    }

    // ── Sync methods ──────────────────────────────────────────

    /// Check engine health. Returns full health status object.
    pub fn health(&self, py: Python<'_>) -> PyResult<PyHealthStatus> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.health()))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Search across indexed repositories.
    ///
    /// Args:
    ///     query: SearchQuery object
    pub fn search(&self, py: Python<'_>, query: PySearchQuery) -> PyResult<PySearchResult> {
        let inner = self.inner.clone();
        let uc_query: uc_types::SearchQuery = query.into();
        let result = py
            .allow_threads(|| async_support::block_on(inner.search(uc_query)))
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
        let inner = self.inner.clone();
        let request = uc_types::IndexRequest {
            repo: uc_types::RepoSpec {
                repo_id,
                remote_url: remote_url.unwrap_or_default(),
                default_branch,
                local_path: Some(local_path),
            },
            force_full,
        };
        let result = py
            .allow_threads(|| async_support::block_on(inner.index_repo(request)))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Get the current index state for a repository.
    pub fn get_index_state(&self, py: Python<'_>, repo_id: String) -> PyResult<PyRepoIndexState> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.get_index_state(&repo_id)))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into())
    }

    /// Get detailed index state for a repository (includes health and version info).
    pub fn get_detailed_index_state(
        &self,
        py: Python<'_>,
        repo_id: String,
    ) -> PyResult<PyIndexState> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.get_index_state(&repo_id)))
            .map_err(engine_error_to_pyerr)?;
        Ok(PyIndexState::from_repo_index_state(result))
    }

    /// Remove a repository's index.
    pub fn remove_index(&self, py: Python<'_>, repo_id: String) -> PyResult<()> {
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.remove_index(&repo_id)))
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
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        let request = uc_types::MemoryReadRequest {
            key: mem_key,
            include_semantic,
        };
        let result = py
            .allow_threads(|| async_support::block_on(inner.read_memory(request)))
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
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        let mem_content = build_memory_content(
            &content_type,
            content,
            language,
            file_path,
            uri,
            description,
        );
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
        let result = py
            .allow_threads(|| async_support::block_on(inner.write_memory(request)))
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
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        py.allow_threads(|| async_support::block_on(inner.delete_memory(&mem_key)))
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
        let inner = self.inner.clone();
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
        let result = py
            .allow_threads(|| async_support::block_on(inner.search_memory(request)))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.results.into_iter().map(Into::into).collect())
    }

    /// Get the current mode.
    #[getter]
    pub fn mode(&self) -> &str {
        &self.mode
    }

    /// Batch write multiple memory entries.
    ///
    /// Args:
    ///     requests: List of dicts, each with keys:
    ///         key_scope, key, content, content_type, source_agent, importance, tags,
    ///         task_id, project_id, language, file_path, uri, description
    ///
    /// Returns list of MemoryEntry objects.
    pub fn batch_write_memory(
        &self,
        py: Python<'_>,
        requests: Vec<pyo3::Bound<'_, pyo3::types::PyDict>>,
    ) -> PyResult<Vec<PyMemoryEntry>> {
        let inner = self.inner.clone();
        let mut write_requests = Vec::with_capacity(requests.len());
        for req_dict in &requests {
            let get_str = |key: &str| -> PyResult<Option<String>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<String>()?)),
                    None => Ok(None),
                }
            };
            let get_f32 = |key: &str| -> PyResult<Option<f32>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<f32>()?)),
                    None => Ok(None),
                }
            };
            let get_str_list = |key: &str| -> PyResult<Option<Vec<String>>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<Vec<String>>()?)),
                    None => Ok(None),
                }
            };

            let key_scope = get_str("key_scope")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("key_scope is required"))?;
            let key = get_str("key")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("key is required"))?;
            let task_id = get_str("task_id")?;
            let project_id = get_str("project_id")?;
            let content = get_str("content")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("content is required"))?;
            let content_type = get_str("content_type")?.unwrap_or_else(|| "text".to_string());
            let source_agent = get_str("source_agent")?.unwrap_or_else(|| "python".to_string());
            let importance = get_f32("importance")?.unwrap_or(0.5);
            let tags = get_str_list("tags")?;
            let language = get_str("language")?;
            let file_path = get_str("file_path")?;
            let uri = get_str("uri")?;
            let description = get_str("description")?;

            let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
            let mem_content = build_memory_content(
                &content_type,
                content,
                language,
                file_path,
                uri,
                description,
            );
            write_requests.push(uc_types::MemoryWriteRequest {
                key: mem_key,
                content: mem_content,
                metadata: uc_types::MemoryMetadata {
                    source_agent,
                    importance,
                    tags: tags.unwrap_or_default(),
                    embedding: None,
                },
            });
        }
        let result = py
            .allow_threads(|| async_support::block_on(inner.batch_write_memory(write_requests)))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into_iter().map(Into::into).collect())
    }

    /// List all indexed repositories.
    ///
    /// Returns list of RepoIndexState objects.
    pub fn list_repos(&self, py: Python<'_>) -> PyResult<Vec<PyRepoIndexState>> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.list_repos()))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into_iter().map(Into::into).collect())
    }

    /// Stream search results, collecting all items into a list.
    ///
    /// Currently returns a single batch (the underlying engine is not truly streaming).
    /// Returns list of PySearchResultItem.
    // ponytail: collect stream into Vec since current stream is single-element;
    // expose as async generator if true streaming is needed later.
    pub fn search_stream(
        &self,
        py: Python<'_>,
        query: PySearchQuery,
    ) -> PyResult<Vec<PySearchResultItem>> {
        let inner = self.inner.clone();
        let uc_query: uc_types::SearchQuery = query.into();
        let results: Vec<uc_types::SearchResult> = py
            .allow_threads(|| {
                async_support::block_on(async {
                    let stream = inner.search_stream(uc_query).await?;
                    Ok::<_, uc_types::EngineError>(futures::StreamExt::collect(stream).await)
                })
            })
            .map_err(engine_error_to_pyerr)?;
        let items: Vec<PySearchResultItem> = results
            .into_iter()
            .flat_map(|r| r.items)
            .map(Into::into)
            .collect();
        Ok(items)
    }

    // ── Task Orchestration ──────────────────────────────────────

    /// Submit a new task for orchestration.
    ///
    /// Args:
    ///     description: Task description
    ///     project_id: Project ID (default: empty string)
    #[pyo3(signature = (description, project_id=None))]
    pub fn submit_task(
        &self,
        py: Python<'_>,
        description: String,
        project_id: Option<String>,
    ) -> PyResult<PyTask> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| {
                async_support::block_on(inner.submit_task(description, project_id.unwrap_or_default()))
            })
            .map_err(engine_error_to_pyerr)?;
        Ok(PyTask::from(result))
    }

    /// Get a task by ID.
    pub fn get_task(&self, py: Python<'_>, task_id: String) -> PyResult<PyTask> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.get_task(&task_id)))
            .map_err(engine_error_to_pyerr)?;
        Ok(PyTask::from(result))
    }

    /// List all tasks.
    pub fn list_tasks(&self, py: Python<'_>) -> PyResult<Vec<PyTask>> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.list_tasks()))
            .map_err(engine_error_to_pyerr)?;
        Ok(result.into_iter().map(PyTask::from).collect())
    }

    /// Pause a running task.
    pub fn pause_task(&self, py: Python<'_>, task_id: String) -> PyResult<PyTask> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.pause_task(&task_id)))
            .map_err(engine_error_to_pyerr)?;
        Ok(PyTask::from(result))
    }

    /// Resume a paused task.
    pub fn resume_task(&self, py: Python<'_>, task_id: String) -> PyResult<PyTask> {
        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.resume_task(&task_id)))
            .map_err(engine_error_to_pyerr)?;
        Ok(PyTask::from(result))
    }

    // ── Async methods ─────────────────────────────────────────

    /// Async version of health(). Returns full HealthStatus.
    pub fn health_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner.health().await.map_err(engine_error_to_pyerr)?;
            Ok(PyHealthStatus::from(result))
        })
    }

    /// Async version of search().
    pub fn search_async<'py>(
        &self,
        py: Python<'py>,
        query: PySearchQuery,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let uc_query: uc_types::SearchQuery = query.into();
        future_into_py(py, async move {
            let result = inner
                .search(uc_query)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PySearchResult::from(result))
        })
    }

    /// Async version of index_repo().
    #[pyo3(signature = (repo_id, local_path, remote_url=None, default_branch="main".to_string(), force_full=false))]
    pub fn index_repo_async<'py>(
        &self,
        py: Python<'py>,
        repo_id: String,
        local_path: String,
        remote_url: Option<String>,
        default_branch: String,
        force_full: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let request = uc_types::IndexRequest {
                repo: uc_types::RepoSpec {
                    repo_id,
                    remote_url: remote_url.unwrap_or_default(),
                    default_branch,
                    local_path: Some(local_path),
                },
                force_full,
            };
            let result = inner
                .index_repo(request)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyIndexResponse::from(result))
        })
    }

    /// Async version of read_memory().
    #[pyo3(signature = (key_scope, key, task_id=None, project_id=None, include_semantic=false))]
    pub fn read_memory_async<'py>(
        &self,
        py: Python<'py>,
        key_scope: String,
        key: String,
        task_id: Option<String>,
        project_id: Option<String>,
        include_semantic: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        let request = uc_types::MemoryReadRequest {
            key: mem_key,
            include_semantic,
        };
        future_into_py(py, async move {
            let result = inner
                .read_memory(request)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(result.map(PyMemoryEntry::from))
        })
    }

    /// Async version of write_memory().
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (key_scope, key, content, content_type="text".to_string(), source_agent="python".to_string(), importance=0.5, tags=None, task_id=None, project_id=None, language=None, file_path=None, uri=None, description=None))]
    pub fn write_memory_async<'py>(
        &self,
        py: Python<'py>,
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
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        let mem_content = build_memory_content(
            &content_type,
            content,
            language,
            file_path,
            uri,
            description,
        );
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
        future_into_py(py, async move {
            let result = inner
                .write_memory(request)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyMemoryEntry::from(result))
        })
    }

    /// Async version of delete_memory().
    #[pyo3(signature = (key_scope, key, task_id=None, project_id=None))]
    pub fn delete_memory_async<'py>(
        &self,
        py: Python<'py>,
        key_scope: String,
        key: String,
        task_id: Option<String>,
        project_id: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
        future_into_py(py, async move {
            inner
                .delete_memory(&mem_key)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(())
        })
    }

    /// Async version of search_memory().
    #[pyo3(signature = (query, scope_type="all".to_string(), project_id=None, max_results=20, min_score=0.5))]
    pub fn search_memory_async<'py>(
        &self,
        py: Python<'py>,
        query: String,
        scope_type: String,
        project_id: Option<String>,
        max_results: u32,
        min_score: f32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
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
        future_into_py(py, async move {
            let result = inner
                .search_memory(request)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(result
                .results
                .into_iter()
                .map(PyMemorySearchResult::from)
                .collect::<Vec<_>>())
        })
    }

    /// Async version of get_index_state().
    pub fn get_index_state_async<'py>(
        &self,
        py: Python<'py>,
        repo_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .get_index_state(&repo_id)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyRepoIndexState::from(result))
        })
    }

    /// Async version of remove_index().
    pub fn remove_index_async<'py>(
        &self,
        py: Python<'py>,
        repo_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner
                .remove_index(&repo_id)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(())
        })
    }

    /// Async version of batch_write_memory().
    pub fn batch_write_memory_async<'py>(
        &self,
        py: Python<'py>,
        requests: Vec<pyo3::Bound<'py, pyo3::types::PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let mut write_requests = Vec::with_capacity(requests.len());
        for req_dict in &requests {
            let get_str = |key: &str| -> PyResult<Option<String>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<String>()?)),
                    None => Ok(None),
                }
            };
            let get_f32 = |key: &str| -> PyResult<Option<f32>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<f32>()?)),
                    None => Ok(None),
                }
            };
            let get_str_list = |key: &str| -> PyResult<Option<Vec<String>>> {
                match req_dict.get_item(key)? {
                    Some(val) => Ok(Some(val.extract::<Vec<String>>()?)),
                    None => Ok(None),
                }
            };
            let key_scope = get_str("key_scope")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("key_scope is required"))?;
            let key = get_str("key")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("key is required"))?;
            let task_id = get_str("task_id")?;
            let project_id = get_str("project_id")?;
            let content = get_str("content")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("content is required"))?;
            let content_type = get_str("content_type")?.unwrap_or_else(|| "text".to_string());
            let source_agent = get_str("source_agent")?.unwrap_or_else(|| "python".to_string());
            let importance = get_f32("importance")?.unwrap_or(0.5);
            let tags = get_str_list("tags")?;
            let language = get_str("language")?;
            let file_path = get_str("file_path")?;
            let uri = get_str("uri")?;
            let description = get_str("description")?;
            let mem_key = build_memory_key(&key_scope, key, task_id, project_id)?;
            let mem_content = build_memory_content(
                &content_type,
                content,
                language,
                file_path,
                uri,
                description,
            );
            write_requests.push(uc_types::MemoryWriteRequest {
                key: mem_key,
                content: mem_content,
                metadata: uc_types::MemoryMetadata {
                    source_agent,
                    importance,
                    tags: tags.unwrap_or_default(),
                    embedding: None,
                },
            });
        }
        future_into_py(py, async move {
            let result = inner
                .batch_write_memory(write_requests)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(result
                .into_iter()
                .map(PyMemoryEntry::from)
                .collect::<Vec<_>>())
        })
    }

    /// Async version of list_repos().
    pub fn list_repos_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner.list_repos().await.map_err(engine_error_to_pyerr)?;
            Ok(result
                .into_iter()
                .map(PyRepoIndexState::from)
                .collect::<Vec<_>>())
        })
    }

    /// Async version of search_stream(). Collects stream into a list.
    pub fn search_stream_async<'py>(
        &self,
        py: Python<'py>,
        query: PySearchQuery,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        let uc_query: uc_types::SearchQuery = query.into();
        future_into_py(py, async move {
            let stream = inner
                .search_stream(uc_query)
                .await
                .map_err(engine_error_to_pyerr)?;
            let results: Vec<uc_types::SearchResult> = futures::StreamExt::collect(stream).await;
            let items: Vec<PySearchResultItem> = results
                .into_iter()
                .flat_map(|r| r.items)
                .map(Into::into)
                .collect();
            Ok(items)
        })
    }

    // ── Async Task Orchestration ──────────────────────────────────

    /// Async version of submit_task().
    #[pyo3(signature = (description, project_id=None))]
    pub fn submit_task_async<'py>(
        &self,
        py: Python<'py>,
        description: String,
        project_id: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .submit_task(description, project_id.unwrap_or_default())
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyTask::from(result))
        })
    }

    /// Async version of get_task().
    pub fn get_task_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .get_task(&task_id)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyTask::from(result))
        })
    }

    /// Async version of list_tasks().
    pub fn list_tasks_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner.list_tasks().await.map_err(engine_error_to_pyerr)?;
            Ok(result.into_iter().map(PyTask::from).collect::<Vec<_>>())
        })
    }

    /// Async version of pause_task().
    pub fn pause_task_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .pause_task(&task_id)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyTask::from(result))
        })
    }

    /// Async version of resume_task().
    pub fn resume_task_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .resume_task(&task_id)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(PyTask::from(result))
        })
    }

    /// Watch a task for events (gRPC mode only).
    ///
    /// Collects events from the server-streaming WatchTask RPC,
    /// returning up to `max_events` events within `timeout_secs`.
    /// Use in a polling loop for continuous monitoring.
    ///
    /// Args:
    ///     task_id: The task to watch (empty string watches all tasks).
    ///     max_events: Maximum events to collect (default: 50).
    ///     timeout_secs: Timeout in seconds (default: 10.0).
    ///
    /// Returns:
    ///     List of dicts with event_type, task_id, event_id, timestamp.
    ///
    /// Raises:
    ///     RuntimeError: If not in gRPC mode.
    #[pyo3(signature = (task_id, max_events=50, timeout_secs=10.0))]
    pub fn watch_task(
        &self,
        py: Python<'_>,
        task_id: String,
        max_events: usize,
        timeout_secs: f64,
    ) -> PyResult<Vec<Py<PyDict>>> {
        let grpc_client = self.grpc_client.as_ref().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("watch_task requires gRPC mode")
        })?;
        let client = grpc_client.clone();
        let events = py.allow_threads(|| {
            async_support::block_on(async {
                let stream = client
                    .watch_task(&task_id)
                    .await
                    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
                collect_events(stream, max_events, timeout_secs).await
            })
        })?;
        Ok(events)
    }

    /// Async version of watch_task().
    ///
    /// Collects events from the server-streaming WatchTask RPC,
    /// returning up to `max_events` events within `timeout_secs`.
    ///
    /// Args:
    ///     task_id: The task to watch (empty string watches all tasks).
    ///     max_events: Maximum events to collect (default: 50).
    ///     timeout_secs: Timeout in seconds (default: 10.0).
    ///
    /// Returns:
    ///     List of dicts with event_type, task_id, event_id, timestamp.
    ///
    /// Raises:
    ///     RuntimeError: If not in gRPC mode.
    #[pyo3(signature = (task_id, max_events=50, timeout_secs=10.0))]
    pub fn watch_task_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
        max_events: usize,
        timeout_secs: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let grpc_client = self.grpc_client.clone();
        let client = grpc_client.ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("watch_task requires gRPC mode")
        })?;
        future_into_py::<_, Vec<Py<PyDict>>>(py, async move {
            let stream = client
                .watch_task(&task_id)
                .await
                .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
            let events = collect_events(stream, max_events, timeout_secs).await?;
            Ok(events)
        })
    }
}
