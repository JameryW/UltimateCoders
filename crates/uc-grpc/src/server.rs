//! gRPC server wrapping an EngineApi implementor + TaskService.
//!
//! Accepts proto requests, converts to uc-types, calls the engine,
//! and converts results back to proto responses.
//!
//! TaskService uses an in-memory task store (bridge until full Python
//! Orchestrator integration).

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::Mutex;
use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::conversions::{memory_key_from_proto, task_status_to_proto};
use crate::ultimate_coders::engine_service_server::{EngineService, EngineServiceServer};
use crate::ultimate_coders::task_service_server::{TaskService, TaskServiceServer};
use crate::ultimate_coders::*;

// ── In-memory task store ────────────────────────────────────

/// In-memory store for tasks and events, used by TaskService.
pub struct TaskStore {
    tasks: HashMap<String, uc_types::Task>,
    events: Vec<uc_engine::AgentEventType>,
}

impl Default for TaskStore {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskStore {
    pub fn new() -> Self {
        Self {
            tasks: HashMap::new(),
            events: Vec::new(),
        }
    }

    /// Submit a new task: create it, decompose into subtasks, store, and return.
    pub fn submit_task(&mut self, description: String, project_id: String) -> uc_types::Task {
        let task_id = uc_types::TaskId::new();
        let now = chrono::Utc::now();

        // Simple decomposition: split by newlines or sentences
        let subtasks = decompose_task(&task_id, &description);

        let task = uc_types::Task {
            id: task_id.clone(),
            description: description.clone(),
            project_id,
            status: uc_types::TaskStatus::InProgress,
            subtasks,
            created_at: now,
            updated_at: now,
        };

        // Record TaskCreated event
        self.events.push(uc_engine::AgentEventType::TaskCreated {
            task_id: task_id.clone(),
            description: description.clone(),
        });

        // Record subtask events
        for st in &task.subtasks {
            self.events
                .push(uc_engine::AgentEventType::SubtaskAssigned {
                    subtask_id: st.id.clone(),
                    worker_id: uc_types::WorkerId::new(),
                });
        }

        let task_id_str = task.id.0.clone();
        self.tasks.insert(task_id_str, task.clone());
        task
    }

    /// Get a task by ID.
    pub fn get_task(&self, task_id: &str) -> Option<&uc_types::Task> {
        self.tasks.get(task_id)
    }

    /// List all tasks.
    pub fn list_tasks(&self) -> Vec<uc_types::Task> {
        self.tasks.values().cloned().collect()
    }

    /// Pause a task. Only tasks in InProgress or Planning status can be paused.
    pub fn pause_task(&mut self, task_id: &str) -> Result<uc_types::Task, String> {
        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            uc_types::TaskStatus::InProgress | uc_types::TaskStatus::Planning => {
                task.status = uc_types::TaskStatus::Paused;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(format!(
                "Cannot pause task in {} status (expected InProgress or Planning)",
                task_status_to_proto(other)
            )),
        }
    }

    /// Resume a task. Only tasks in Paused status can be resumed.
    pub fn resume_task(&mut self, task_id: &str) -> Result<uc_types::Task, String> {
        let task = self
            .tasks
            .get_mut(task_id)
            .ok_or_else(|| format!("Task not found: {}", task_id))?;
        match &task.status {
            uc_types::TaskStatus::Paused => {
                task.status = uc_types::TaskStatus::InProgress;
                task.updated_at = chrono::Utc::now();
                Ok(task.clone())
            }
            other => Err(format!(
                "Cannot resume task in {} status (expected Paused)",
                task_status_to_proto(other)
            )),
        }
    }

    /// Read events from the given offset.
    pub fn read_events_from(&self, offset: usize) -> Vec<uc_engine::AgentEventType> {
        if offset >= self.events.len() {
            Vec::new()
        } else {
            self.events[offset..].to_vec()
        }
    }

    /// Get current event count (used as latest offset).
    pub fn event_count(&self) -> usize {
        self.events.len()
    }
}

/// Simple task decomposition heuristic: split description by newlines
/// or numbered items, creating one subtask per line/item.
fn decompose_task(parent_id: &uc_types::TaskId, description: &str) -> Vec<uc_types::Subtask> {
    let lines: Vec<&str> = description
        .lines()
        .map(|l| l.trim())
        .filter(|l| !l.is_empty())
        .collect();

    if lines.is_empty() {
        // Single subtask if description has no newlines
        return vec![uc_types::Subtask {
            id: uc_types::TaskId::new(),
            parent_id: parent_id.clone(),
            description: description.to_string(),
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: Vec::new(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
        }];
    }

    // Create subtasks from lines, with sequential dependencies
    let mut subtasks = Vec::new();
    let mut prev_id: Option<uc_types::TaskId> = None;

    for (i, line) in lines.iter().enumerate() {
        // Strip leading numbers like "1. " or "1) "
        let cleaned = line
            .trim_start_matches(|c: char| c.is_numeric())
            .trim_start_matches(['.', ')', ' '])
            .to_string();

        let desc = if cleaned.is_empty() {
            line.to_string()
        } else {
            cleaned
        };

        let st_id = uc_types::TaskId::new();
        let depends_on = if i > 0 {
            prev_id
                .as_ref()
                .map(|id| vec![id.clone()])
                .unwrap_or_default()
        } else {
            Vec::new()
        };

        subtasks.push(uc_types::Subtask {
            id: st_id.clone(),
            parent_id: parent_id.clone(),
            description: desc,
            status: uc_types::SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on,
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
        });

        prev_id = Some(st_id);
    }

    subtasks
}

// ── gRPC Server ─────────────────────────────────────────────

/// Internal shared state for the gRPC server.
struct GrpcServerInner<E: EngineApi + Send + Sync + 'static> {
    engine: E,
    task_store: Arc<Mutex<TaskStore>>,
}

/// gRPC server that delegates EngineService operations to an inner EngineApi
/// and provides TaskService via an in-memory task store.
///
/// Internally wraps state in `Arc` so both services can share it.
pub struct GrpcServer<E: EngineApi + Send + Sync + 'static> {
    inner: Arc<GrpcServerInner<E>>,
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Create a new gRPC server wrapping the given engine.
    pub fn new(engine: E) -> Self {
        Self {
            inner: Arc::new(GrpcServerInner {
                engine,
                task_store: Arc::new(Mutex::new(TaskStore::new())),
            }),
        }
    }

    /// Convert into tonic services ready to be served.
    pub fn into_services(self) -> (EngineServiceServer<Self>, TaskServiceServer<Self>) {
        let engine_service = EngineServiceServer::new(Self {
            inner: self.inner.clone(),
        });
        let task_service = TaskServiceServer::new(self);
        (engine_service, task_service)
    }
}

impl<E: EngineApi + Send + Sync + 'static> Clone for GrpcServer<E> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

fn to_status(err: uc_types::EngineError) -> Status {
    use uc_types::EngineError::*;
    let (code, msg) = match &err {
        SearchError(m) => (tonic::Code::Internal, m.clone()),
        IndexError(m) => (tonic::Code::NotFound, m.clone()),
        MemoryReadError(m) | MemoryWriteError(m) => (tonic::Code::Internal, m.clone()),
        IndexingError(m) => (tonic::Code::Internal, m.clone()),
        ConnectionError(m) => (tonic::Code::Unavailable, m.clone()),
        TimeoutError(m) => (tonic::Code::DeadlineExceeded, m.clone()),
        RateLimited(secs) => (
            tonic::Code::ResourceExhausted,
            format!("retry after {}s", secs),
        ),
        ConflictError { path, details } => (tonic::Code::Aborted, format!("{}: {}", path, details)),
        TaskError(m) => (tonic::Code::FailedPrecondition, m.clone()),
        WorkerUnavailable(m) => (tonic::Code::Unavailable, m.clone()),
        SandboxError(m) => (tonic::Code::PermissionDenied, m.clone()),
        ConfigError(m) => (tonic::Code::InvalidArgument, m.clone()),
        InternalError(m) => (tonic::Code::Internal, m.clone()),
    };
    Status::new(code, msg)
}

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> EngineService for GrpcServer<E> {
    async fn search(
        &self,
        request: Request<SearchRequest>,
    ) -> Result<Response<SearchResponse>, Status> {
        let req = request.into_inner();
        let query: uc_types::SearchQuery = req.into();
        let result = self.inner.engine.search(query).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn index_repo(
        &self,
        request: Request<IndexRepoRequest>,
    ) -> Result<Response<IndexRepoResponse>, Status> {
        let req = request.into_inner();
        let index_req: uc_types::IndexRequest = req.into();
        let result = self
            .inner
            .engine
            .index_repo(index_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn get_index_state(
        &self,
        request: Request<GetIndexStateRequest>,
    ) -> Result<Response<GetIndexStateResponse>, Status> {
        let req = request.into_inner();
        let repo_id = req.repo_id.clone();
        let result = self
            .inner
            .engine
            .get_index_state(&repo_id)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn remove_index(
        &self,
        request: Request<RemoveIndexRequest>,
    ) -> Result<Response<RemoveIndexResponse>, Status> {
        let req = request.into_inner();
        self.inner
            .engine
            .remove_index(&req.repo_id)
            .await
            .map_err(to_status)?;
        Ok(Response::new(RemoveIndexResponse {}))
    }

    async fn read_memory(
        &self,
        request: Request<ReadMemoryRequest>,
    ) -> Result<Response<ReadMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        let read_req = uc_types::MemoryReadRequest {
            key,
            include_semantic: req.include_semantic,
        };
        let result = self
            .inner
            .engine
            .read_memory(read_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn write_memory(
        &self,
        request: Request<WriteMemoryRequest>,
    ) -> Result<Response<WriteMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        let content = match req.content_type.as_str() {
            "structured" => uc_types::MemoryContent::Structured(
                serde_json::from_str(&req.content)
                    .unwrap_or(serde_json::Value::String(req.content.clone())),
            ),
            "code" => uc_types::MemoryContent::Code {
                language: req.language.unwrap_or_default(),
                code: req.content,
            },
            "diff" => uc_types::MemoryContent::Diff {
                file_path: req.file_path.unwrap_or_default(),
                diff: req.content,
            },
            "reference" => uc_types::MemoryContent::Reference {
                uri: req.uri.unwrap_or_default(),
                description: req.description.unwrap_or_default(),
            },
            _ => uc_types::MemoryContent::Text(req.content),
        };
        let write_req = uc_types::MemoryWriteRequest {
            key,
            content,
            metadata: uc_types::MemoryMetadata {
                source_agent: req.source_agent,
                importance: req.importance,
                tags: req.tags,
                embedding: None,
            },
        };
        let result = self
            .inner
            .engine
            .write_memory(write_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn delete_memory(
        &self,
        request: Request<DeleteMemoryRequest>,
    ) -> Result<Response<DeleteMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        self.inner
            .engine
            .delete_memory(&key)
            .await
            .map_err(to_status)?;
        Ok(Response::new(DeleteMemoryResponse {}))
    }

    async fn search_memory(
        &self,
        request: Request<SearchMemoryRequest>,
    ) -> Result<Response<SearchMemoryResponse>, Status> {
        let req = request.into_inner();
        let search_req: uc_types::MemorySearchRequest = req.into();
        let result = self
            .inner
            .engine
            .search_memory(search_req)
            .await
            .map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let result = self.inner.engine.health().await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }
}

#[tonic::async_trait]
impl<E: EngineApi + Send + Sync + 'static> TaskService for GrpcServer<E> {
    async fn submit_task(
        &self,
        request: Request<SubmitTaskRequest>,
    ) -> Result<Response<SubmitTaskResponse>, Status> {
        let req = request.into_inner();
        let mut store = self.inner.task_store.lock().await;
        let task = store.submit_task(req.description, req.project_id);

        let subtask_protos: Vec<SubtaskProto> =
            task.subtasks.clone().into_iter().map(Into::into).collect();

        Ok(Response::new(SubmitTaskResponse {
            success: true,
            task_id: task.id.0,
            status: task_status_to_proto(&task.status).to_string(),
            subtask_count: task.subtasks.len() as u32,
            subtasks: subtask_protos,
            error: None,
        }))
    }

    async fn get_task(
        &self,
        request: Request<GetTaskRequest>,
    ) -> Result<Response<GetTaskResponse>, Status> {
        let req = request.into_inner();
        let store = self.inner.task_store.lock().await;
        match store.get_task(&req.task_id) {
            Some(task) => Ok(Response::new(GetTaskResponse {
                available: true,
                task: Some(task.clone().into()),
            })),
            None => Ok(Response::new(GetTaskResponse {
                available: false,
                task: None,
            })),
        }
    }

    async fn list_tasks(
        &self,
        _request: Request<ListTasksRequest>,
    ) -> Result<Response<ListTasksResponse>, Status> {
        let store = self.inner.task_store.lock().await;
        let tasks: Vec<TaskProto> = store.list_tasks().into_iter().map(Into::into).collect();
        let total = tasks.len() as u32;

        // Compute status counts
        let mut status_counts: HashMap<String, u32> = HashMap::new();
        for task in &tasks {
            *status_counts.entry(task.status.clone()).or_insert(0) += 1;
        }

        Ok(Response::new(ListTasksResponse {
            available: true,
            tasks,
            total,
            status_counts,
        }))
    }

    type WatchTaskStream =
        std::pin::Pin<Box<dyn tokio_stream::Stream<Item = Result<TaskEvent, Status>> + Send>>;

    async fn watch_task(
        &self,
        request: Request<WatchTaskRequest>,
    ) -> Result<Response<Self::WatchTaskStream>, Status> {
        let req = request.into_inner();
        let task_id = req.task_id;
        let task_store = self.inner.task_store.clone();

        // Create a polling stream that checks for new events every 500ms
        let stream = async_stream::stream! {
            let mut offset = 0usize;

            loop {
                let events = {
                    let s = task_store.lock().await;
                    s.read_events_from(offset)
                };

                for event in events {
                    let proto_event: TaskEvent = event.into();

                    // Filter by task_id if specified.
                    // TODO: Subtask-level events (SubtaskAssigned, SubtaskStarted, etc.)
                    // currently have an empty task_id in the proto conversion because
                    // AgentEventType variants do not carry a task_id field. This means
                    // watching a specific task_id will miss subtask events. The TUI
                    // currently uses empty task_id (watch all), so this is not an
                    // immediate issue. A future fix should maintain a subtask->task
                    // mapping and populate the task_id for subtask events.
                    if !task_id.is_empty() && proto_event.task_id != task_id {
                        offset += 1;
                        continue;
                    }

                    offset += 1;
                    yield Ok(proto_event);
                }

                // Wait before polling again
                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            }
        };

        Ok(Response::new(Box::pin(stream)))
    }

    async fn pause_task(
        &self,
        request: Request<PauseTaskRequest>,
    ) -> Result<Response<PauseTaskResponse>, Status> {
        let req = request.into_inner();
        let mut store = self.inner.task_store.lock().await;
        match store.pause_task(&req.task_id) {
            Ok(task) => Ok(Response::new(PauseTaskResponse {
                success: true,
                task_id: task.id.0,
                status: task_status_to_proto(&task.status).to_string(),
                error: None,
            })),
            Err(e) => Ok(Response::new(PauseTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }

    async fn resume_task(
        &self,
        request: Request<ResumeTaskRequest>,
    ) -> Result<Response<ResumeTaskResponse>, Status> {
        let req = request.into_inner();
        let mut store = self.inner.task_store.lock().await;
        match store.resume_task(&req.task_id) {
            Ok(task) => Ok(Response::new(ResumeTaskResponse {
                success: true,
                task_id: task.id.0,
                status: task_status_to_proto(&task.status).to_string(),
                error: None,
            })),
            Err(e) => Ok(Response::new(ResumeTaskResponse {
                success: false,
                task_id: req.task_id,
                status: String::new(),
                error: Some(e),
            })),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_mapping_search() {
        let status = to_status(uc_types::EngineError::SearchError("test".into()));
        assert_eq!(status.code(), tonic::Code::Internal);
    }

    #[test]
    fn error_mapping_index_not_found() {
        let status = to_status(uc_types::EngineError::IndexError("repo-1".into()));
        assert_eq!(status.code(), tonic::Code::NotFound);
    }

    #[test]
    fn error_mapping_connection() {
        let status = to_status(uc_types::EngineError::ConnectionError("refused".into()));
        assert_eq!(status.code(), tonic::Code::Unavailable);
    }

    #[test]
    fn error_mapping_timeout() {
        let status = to_status(uc_types::EngineError::TimeoutError("30s".into()));
        assert_eq!(status.code(), tonic::Code::DeadlineExceeded);
    }

    #[test]
    fn error_mapping_rate_limited() {
        let status = to_status(uc_types::EngineError::RateLimited(5));
        assert_eq!(status.code(), tonic::Code::ResourceExhausted);
    }

    #[test]
    fn error_mapping_conflict() {
        let status = to_status(uc_types::EngineError::ConflictError {
            path: "src/main.rs".into(),
            details: "overlap".into(),
        });
        assert_eq!(status.code(), tonic::Code::Aborted);
    }

    #[test]
    fn task_store_submit_and_get() {
        let mut store = TaskStore::new();
        let task = store.submit_task(
            "1. Analyze code\n2. Fix bug\n3. Write tests".to_string(),
            "project-1".to_string(),
        );

        assert_eq!(task.subtasks.len(), 3);
        assert_eq!(task.status, uc_types::TaskStatus::InProgress);

        // Get the task back
        let retrieved = store.get_task(&task.id.0).unwrap();
        assert_eq!(
            retrieved.description,
            "1. Analyze code\n2. Fix bug\n3. Write tests"
        );
    }

    #[test]
    fn task_store_pause_and_resume() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, uc_types::TaskStatus::Paused);

        let resumed = store.resume_task(&task_id).unwrap();
        assert_eq!(resumed.status, uc_types::TaskStatus::InProgress);
    }

    #[test]
    fn task_store_pause_nonexistent() {
        let mut store = TaskStore::new();
        let result = store.pause_task("nonexistent");
        assert!(result.is_err());
    }

    #[test]
    fn task_store_pause_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Pause (valid: InProgress -> Paused)
        let paused = store.pause_task(&task_id).unwrap();
        assert_eq!(paused.status, uc_types::TaskStatus::Paused);

        // Pause again (invalid: Paused -> Paused)
        let result = store.pause_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_resume_invalid_status() {
        let mut store = TaskStore::new();
        let task = store.submit_task("Test task".to_string(), "p1".to_string());
        let task_id = task.id.0.clone();

        // Resume without pausing first (invalid: InProgress -> InProgress)
        let result = store.resume_task(&task_id);
        assert!(result.is_err());
    }

    #[test]
    fn task_store_list_tasks() {
        let mut store = TaskStore::new();
        store.submit_task("Task 1".to_string(), "p1".to_string());
        store.submit_task("Task 2".to_string(), "p1".to_string());

        let tasks = store.list_tasks();
        assert_eq!(tasks.len(), 2);
    }

    #[test]
    fn task_store_events() {
        let mut store = TaskStore::new();
        store.submit_task("Test task".to_string(), "p1".to_string());

        // Should have TaskCreated + SubtaskAssigned events
        assert!(store.event_count() >= 2);

        // Read from offset 0
        let events = store.read_events_from(0);
        assert!(!events.is_empty());

        // Read from beyond end
        let events = store.read_events_from(100);
        assert!(events.is_empty());
    }

    #[test]
    fn decompose_task_single_line() {
        let parent_id = uc_types::TaskId::new();
        let subtasks = decompose_task(&parent_id, "Single task description");
        assert_eq!(subtasks.len(), 1);
        assert_eq!(subtasks[0].description, "Single task description");
    }

    #[test]
    fn decompose_task_multiple_lines() {
        let parent_id = uc_types::TaskId::new();
        let subtasks = decompose_task(&parent_id, "1. First item\n2. Second item\n3. Third item");
        assert_eq!(subtasks.len(), 3);
        // Check that numbered prefixes are stripped
        assert_eq!(subtasks[0].description, "First item");
        assert_eq!(subtasks[1].description, "Second item");
        // Check sequential dependencies
        assert!(subtasks[0].depends_on.is_empty());
        assert_eq!(subtasks[1].depends_on.len(), 1);
    }

    #[test]
    fn task_status_to_proto_conversion() {
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Created),
            "Created"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::InProgress),
            "InProgress"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Paused),
            "Paused"
        );
        assert_eq!(
            task_status_to_proto(&uc_types::TaskStatus::Completed),
            "Completed"
        );
    }

    #[test]
    fn subtask_status_to_proto_conversion() {
        use crate::conversions::subtask_status_to_proto;
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::Pending),
            "Pending"
        );
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::InProgress),
            "InProgress"
        );
        assert_eq!(
            subtask_status_to_proto(&uc_types::SubtaskStatus::Conflicted),
            "Conflicted"
        );
    }
}
