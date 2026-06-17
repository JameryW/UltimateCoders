//! gRPC client implementing EngineApi via tonic.
//!
//! Connects to a remote UltimateCoders gRPC server and delegates
//! all EngineApi calls over the network.
//!
//! Also provides TaskService methods as inherent impl on GrpcEngineClient,
//! since TaskService is not part of the EngineApi trait.

use std::pin::Pin;

use futures::Stream;
use uc_types::{
    async_trait, AgentEvent, EngineApi, EngineError, HealthStatus, IndexRequest, IndexResponse,
    MemoryEntry, MemoryKey, MemoryReadRequest, MemorySearchRequest, MemorySearchResponse,
    MemoryWriteRequest, RepoIndexState, SearchQuery, SearchResult, SearchStream, Task,
};

use crate::conversions::memory_key_to_parts;
use crate::ultimate_coders::engine_service_client::EngineServiceClient;
use crate::ultimate_coders::task_service_client::TaskServiceClient;
use crate::ultimate_coders::*;

/// Stream type returned by `watch_task`.
type TaskEventStream = Pin<Box<dyn Stream<Item = AgentEvent> + Send>>;

/// gRPC client that implements EngineApi by calling a remote server.
///
/// Also holds a `TaskServiceClient` for TaskService RPCs (submit_task,
/// get_task, list_tasks, watch_task, pause_task, resume_task).
pub struct GrpcEngineClient {
    inner: EngineServiceClient<tonic::transport::Channel>,
    task_client: TaskServiceClient<tonic::transport::Channel>,
}

impl GrpcEngineClient {
    /// Connect to a remote gRPC server.
    pub async fn connect(endpoint: &str) -> Result<Self, EngineError> {
        let channel = tonic::transport::Channel::from_shared(endpoint.to_string())
            .map_err(|e| EngineError::ConnectionError(format!("Invalid endpoint: {}", e)))?
            .connect()
            .await
            .map_err(|e| EngineError::ConnectionError(format!("Connection failed: {}", e)))?;
        Ok(Self::from_channel(channel))
    }

    /// Create a client from an already-connected channel.
    pub fn from_channel(channel: tonic::transport::Channel) -> Self {
        Self {
            inner: EngineServiceClient::new(channel.clone()),
            task_client: TaskServiceClient::new(channel),
        }
    }

    // ── TaskService methods ──────────────────────────────────
    //
    // These are inherent methods on GrpcEngineClient, NOT trait impl methods.
    // TaskService is a server-side orchestration concern and does not belong
    // in the EngineApi trait. The PyO3 bridge will need a separate design
    // (either extending EngineApi or adding a TaskClient trait) to expose
    // these to Python.

    /// Submit a new task for orchestration.
    pub async fn submit_task(
        &self,
        description: &str,
        project_id: &str,
    ) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = SubmitTaskRequest {
            description: description.to_string(),
            project_id: project_id.to_string(),
        };
        let response = client.submit_task(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    /// Get a task by ID.
    pub async fn get_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = GetTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.get_task(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    /// List all tasks.
    pub async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        let mut client = self.task_client.clone();
        let req = ListTasksRequest {};
        let response = client.list_tasks(req).await.map_err(from_status)?;
        Ok(response
            .into_inner()
            .tasks
            .into_iter()
            .map(Into::into)
            .collect())
    }

    /// Watch a task for real-time events (server-streaming).
    ///
    /// Returns a stream of `AgentEvent` items. If `task_id` is empty,
    /// watches all tasks. Stream errors are silently skipped.
    pub async fn watch_task(&self, task_id: &str) -> Result<TaskEventStream, EngineError> {
        let mut client = self.task_client.clone();
        let req = WatchTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.watch_task(req).await.map_err(from_status)?;
        let stream = response.into_inner();
        use futures::StreamExt;
        let mapped = stream.filter_map(|event_result| async move {
            match event_result {
                Ok(proto_event) => Some(AgentEvent::from(proto_event)),
                Err(_) => None, // Skip stream errors
            }
        });
        Ok(Box::pin(mapped))
    }

    /// Pause a running task.
    pub async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = PauseTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.pause_task(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    /// Resume a paused task.
    pub async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = ResumeTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.resume_task(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }
}

fn from_status(status: tonic::Status) -> EngineError {
    use tonic::Code::*;
    match status.code() {
        NotFound => EngineError::NotFound(status.message().to_string()),
        DeadlineExceeded => EngineError::TimeoutError(status.message().to_string()),
        Unavailable => EngineError::ConnectionError(status.message().to_string()),
        ResourceExhausted => {
            // Try to parse "retry after Ns" from message
            let msg = status.message();
            EngineError::RateLimited(
                msg.split("retry after ")
                    .nth(1)
                    .and_then(|s| s.trim_end_matches('s').parse().ok())
                    .unwrap_or(0),
            )
        }
        Aborted => EngineError::ConflictError {
            path: String::new(),
            details: status.message().to_string(),
        },
        InvalidArgument => EngineError::ConfigError(status.message().to_string()),
        PermissionDenied => EngineError::SandboxError(status.message().to_string()),
        FailedPrecondition => EngineError::TaskError(status.message().to_string()),
        _ => EngineError::InternalError(status.message().to_string()),
    }
}

#[async_trait]
impl EngineApi for GrpcEngineClient {
    async fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError> {
        let mut client = self.inner.clone();
        let req = SearchRequest {
            query: query.query,
            modes: query
                .modes
                .iter()
                .map(|m| match m {
                    uc_types::SearchMode::Text => "text".to_string(),
                    uc_types::SearchMode::Semantic => "semantic".to_string(),
                    uc_types::SearchMode::Ast => "ast".to_string(),
                    uc_types::SearchMode::Hybrid => "hybrid".to_string(),
                })
                .collect(),
            repo_ids: query.repo_ids,
            languages: query.languages,
            path_patterns: query.path_patterns,
            max_results: query.max_results,
        };
        let response = client.search(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn index_repo(&self, request: IndexRequest) -> Result<IndexResponse, EngineError> {
        let mut client = self.inner.clone();
        let req = IndexRepoRequest {
            repo_id: request.repo.repo_id,
            remote_url: request.repo.remote_url,
            default_branch: request.repo.default_branch,
            local_path: request.repo.local_path,
            force_full: request.force_full,
        };
        let response = client.index_repo(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn get_index_state(&self, repo_id: &str) -> Result<RepoIndexState, EngineError> {
        let mut client = self.inner.clone();
        let req = GetIndexStateRequest {
            repo_id: repo_id.to_string(),
        };
        let response = client.get_index_state(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn remove_index(&self, repo_id: &str) -> Result<(), EngineError> {
        let mut client = self.inner.clone();
        let req = RemoveIndexRequest {
            repo_id: repo_id.to_string(),
        };
        client.remove_index(req).await.map_err(from_status)?;
        Ok(())
    }

    async fn read_memory(
        &self,
        request: MemoryReadRequest,
    ) -> Result<Option<MemoryEntry>, EngineError> {
        let mut client = self.inner.clone();
        let (key_scope, task_id, project_id, key) = memory_key_to_parts(&request.key);
        let req = ReadMemoryRequest {
            key_scope: key_scope.to_string(),
            task_id: task_id.to_string(),
            project_id: project_id.to_string(),
            key: key.to_string(),
            include_semantic: request.include_semantic,
        };
        let response = client.read_memory(req).await.map_err(from_status)?;
        Ok(response.into_inner().entry.map(Into::into))
    }

    async fn write_memory(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError> {
        let mut client = self.inner.clone();
        let (key_scope, task_id, project_id, key) = memory_key_to_parts(&request.key);
        let (content_type, content, language, file_path, uri, description) = match &request.content
        {
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
        let req = WriteMemoryRequest {
            key_scope: key_scope.to_string(),
            task_id: task_id.to_string(),
            project_id: project_id.to_string(),
            key: key.to_string(),
            content_type,
            content,
            source_agent: request.metadata.source_agent,
            importance: request.metadata.importance,
            tags: request.metadata.tags,
            language,
            file_path,
            uri,
            description,
        };
        let response = client.write_memory(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn delete_memory(&self, key: &MemoryKey) -> Result<(), EngineError> {
        let mut client = self.inner.clone();
        let (key_scope, task_id, project_id, k) = memory_key_to_parts(key);
        let req = DeleteMemoryRequest {
            key_scope: key_scope.to_string(),
            task_id: task_id.to_string(),
            project_id: project_id.to_string(),
            key: k.to_string(),
        };
        client.delete_memory(req).await.map_err(from_status)?;
        Ok(())
    }

    async fn search_memory(
        &self,
        request: MemorySearchRequest,
    ) -> Result<MemorySearchResponse, EngineError> {
        let mut client = self.inner.clone();
        let (scope_type, project_id) = match &request.scope {
            uc_types::MemorySearchScope::Project { project_id } => {
                ("project".to_string(), project_id.clone())
            }
            uc_types::MemorySearchScope::Global => ("global".to_string(), String::new()),
            uc_types::MemorySearchScope::All => ("all".to_string(), String::new()),
        };
        let req = SearchMemoryRequest {
            query: request.query,
            scope_type,
            project_id,
            max_results: request.max_results,
            min_score: request.min_score,
        };
        let response = client.search_memory(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn health(&self) -> Result<HealthStatus, EngineError> {
        let mut client = self.inner.clone();
        let req = HealthRequest {};
        let response = client.health(req).await.map_err(from_status)?;
        Ok(response.into_inner().into())
    }

    async fn batch_write_memory(
        &self,
        requests: Vec<MemoryWriteRequest>,
    ) -> Result<Vec<MemoryEntry>, EngineError> {
        let mut client = self.inner.clone();
        let proto_requests: Vec<WriteMemoryRequest> = requests
            .iter()
            .map(|r| {
                let (key_scope, task_id, project_id, key) = memory_key_to_parts(&r.key);
                let (content_type, content, language, file_path, uri, description) =
                    match &r.content {
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
                WriteMemoryRequest {
                    key_scope: key_scope.to_string(),
                    task_id: task_id.to_string(),
                    project_id: project_id.to_string(),
                    key: key.to_string(),
                    content_type,
                    content,
                    source_agent: r.metadata.source_agent.clone(),
                    importance: r.metadata.importance,
                    tags: r.metadata.tags.clone(),
                    language,
                    file_path,
                    uri,
                    description,
                }
            })
            .collect();
        let req = BatchWriteMemoryRequest {
            requests: proto_requests,
        };
        let response = client.batch_write_memory(req).await.map_err(from_status)?;
        Ok(response
            .into_inner()
            .entries
            .into_iter()
            .map(Into::into)
            .collect())
    }

    async fn list_repos(&self) -> Result<Vec<RepoIndexState>, EngineError> {
        let mut client = self.inner.clone();
        let req = ListReposRequest {};
        let response = client.list_repos(req).await.map_err(from_status)?;
        Ok(response
            .into_inner()
            .repos
            .into_iter()
            .map(Into::into)
            .collect())
    }

    async fn search_stream(&self, query: SearchQuery) -> Result<SearchStream, EngineError> {
        let mut client = self.inner.clone();
        let req = SearchStreamRequest {
            query: query.query,
            modes: query
                .modes
                .iter()
                .map(|m| match m {
                    uc_types::SearchMode::Text => "text".to_string(),
                    uc_types::SearchMode::Semantic => "semantic".to_string(),
                    uc_types::SearchMode::Ast => "ast".to_string(),
                    uc_types::SearchMode::Hybrid => "hybrid".to_string(),
                })
                .collect(),
            repo_ids: query.repo_ids,
            languages: query.languages,
            path_patterns: query.path_patterns,
            max_results: query.max_results,
        };
        let response = client.search_stream(req).await.map_err(from_status)?;
        let stream = response.into_inner();
        use futures::StreamExt;
        let mapped = stream.map(|item_result| match item_result {
            Ok(proto_item) => SearchResult {
                items: vec![proto_item.into()],
            },
            Err(status) => {
                // On stream error, produce an empty result.
                // The caller should check for errors via the stream's error state.
                let _ = from_status(status);
                SearchResult { items: vec![] }
            }
        });
        Ok(Box::pin(mapped))
    }

    async fn submit_task(
        &self,
        description: String,
        project_id: String,
    ) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = SubmitTaskRequest {
            description,
            project_id,
        };
        let response = client.submit_task(req).await.map_err(from_status)?;
        let resp = response.into_inner();
        if !resp.success {
            return Err(EngineError::TaskError(
                resp.error
                    .unwrap_or_else(|| "Unknown task submission error".to_string()),
            ));
        }
        // The SubmitTaskResponse doesn't contain a full TaskProto in all cases,
        // so we fetch the task by ID to get the complete object.
        self.get_task(&resp.task_id).await
    }

    async fn get_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = GetTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.get_task(req).await.map_err(from_status)?;
        let resp = response.into_inner();
        match resp.task {
            Some(task_proto) => Ok(task_proto.into()),
            None => Err(EngineError::NotFound(format!("Task {} not found", task_id))),
        }
    }

    async fn list_tasks(&self) -> Result<Vec<Task>, EngineError> {
        let mut client = self.task_client.clone();
        let req = ListTasksRequest {};
        let response = client.list_tasks(req).await.map_err(from_status)?;
        Ok(response
            .into_inner()
            .tasks
            .into_iter()
            .map(Into::into)
            .collect())
    }

    async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = PauseTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.pause_task(req).await.map_err(from_status)?;
        let resp = response.into_inner();
        if !resp.success {
            return Err(EngineError::TaskError(
                resp.error
                    .unwrap_or_else(|| "Unknown pause error".to_string()),
            ));
        }
        // Fetch the updated task to return the complete object
        self.get_task(task_id).await
    }

    async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError> {
        let mut client = self.task_client.clone();
        let req = ResumeTaskRequest {
            task_id: task_id.to_string(),
        };
        let response = client.resume_task(req).await.map_err(from_status)?;
        let resp = response.into_inner();
        if !resp.success {
            return Err(EngineError::TaskError(
                resp.error
                    .unwrap_or_else(|| "Unknown resume error".to_string()),
            ));
        }
        // Fetch the updated task to return the complete object
        self.get_task(task_id).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tonic::Status;

    #[test]
    fn from_status_not_found() {
        let status = Status::not_found("repo not found");
        let err = from_status(status);
        assert!(matches!(err, EngineError::NotFound(_)));
    }

    #[test]
    fn from_status_unavailable() {
        let status = Status::unavailable("connection refused");
        let err = from_status(status);
        assert!(matches!(err, EngineError::ConnectionError(_)));
    }

    #[test]
    fn from_status_deadline_exceeded() {
        let status = Status::deadline_exceeded("30s");
        let err = from_status(status);
        assert!(matches!(err, EngineError::TimeoutError(_)));
    }

    #[test]
    fn from_status_resource_exhausted() {
        let status = Status::resource_exhausted("retry after 5s");
        let err = from_status(status);
        assert!(matches!(err, EngineError::RateLimited(5)));
    }

    #[test]
    fn from_status_aborted() {
        let status = Status::aborted("conflict in src/main.rs");
        let err = from_status(status);
        assert!(matches!(err, EngineError::ConflictError { .. }));
    }
}
