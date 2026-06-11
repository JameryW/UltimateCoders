//! gRPC server wrapping an EngineApi implementor.
//!
//! Accepts proto requests, converts to uc-types, calls the engine,
//! and converts results back to proto responses.

use tonic::{Request, Response, Status};
use uc_types::EngineApi;

use crate::conversions::memory_key_from_proto;
use crate::ultimate_coders::engine_service_server::{EngineService, EngineServiceServer};
use crate::ultimate_coders::*;

/// gRPC server that delegates all operations to an inner EngineApi.
pub struct GrpcServer<E: EngineApi + Send + Sync + 'static> {
    engine: E,
}

impl<E: EngineApi + Send + Sync + 'static> GrpcServer<E> {
    /// Create a new gRPC server wrapping the given engine.
    pub fn new(engine: E) -> Self {
        Self { engine }
    }

    /// Convert into a tonic service ready to be served.
    pub fn into_service(self) -> EngineServiceServer<Self> {
        EngineServiceServer::new(self)
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
        RateLimited(secs) => (tonic::Code::ResourceExhausted, format!("retry after {}s", secs)),
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
        let result = self.engine.search(query).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn index_repo(
        &self,
        request: Request<IndexRepoRequest>,
    ) -> Result<Response<IndexRepoResponse>, Status> {
        let req = request.into_inner();
        let index_req: uc_types::IndexRequest = req.into();
        let result = self.engine.index_repo(index_req).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn get_index_state(
        &self,
        request: Request<GetIndexStateRequest>,
    ) -> Result<Response<GetIndexStateResponse>, Status> {
        let req = request.into_inner();
        let repo_id = req.repo_id.clone();
        let result = self.engine.get_index_state(&repo_id).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn remove_index(
        &self,
        request: Request<RemoveIndexRequest>,
    ) -> Result<Response<RemoveIndexResponse>, Status> {
        let req = request.into_inner();
        self.engine.remove_index(&req.repo_id).await.map_err(to_status)?;
        Ok(Response::new(RemoveIndexResponse {}))
    }

    async fn read_memory(
        &self,
        request: Request<ReadMemoryRequest>,
    ) -> Result<Response<ReadMemoryResponse>, Status> {
        let req = request.into_inner();
        // Validate key_scope before converting
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        let read_req = uc_types::MemoryReadRequest {
            key,
            include_semantic: req.include_semantic,
        };
        let result = self.engine.read_memory(read_req).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn write_memory(
        &self,
        request: Request<WriteMemoryRequest>,
    ) -> Result<Response<WriteMemoryResponse>, Status> {
        let req = request.into_inner();
        // Validate key_scope before converting
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
        let result = self.engine.write_memory(write_req).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn delete_memory(
        &self,
        request: Request<DeleteMemoryRequest>,
    ) -> Result<Response<DeleteMemoryResponse>, Status> {
        let req = request.into_inner();
        let key = memory_key_from_proto(&req.key_scope, &req.task_id, &req.project_id, &req.key)
            .map_err(Status::invalid_argument)?;
        self.engine.delete_memory(&key).await.map_err(to_status)?;
        Ok(Response::new(DeleteMemoryResponse {}))
    }

    async fn search_memory(
        &self,
        request: Request<SearchMemoryRequest>,
    ) -> Result<Response<SearchMemoryResponse>, Status> {
        let req = request.into_inner();
        let search_req: uc_types::MemorySearchRequest = req.into();
        let result = self.engine.search_memory(search_req).await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let result = self.engine.health().await.map_err(to_status)?;
        Ok(Response::new(result.into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::EngineError;

    #[test]
    fn error_mapping_search() {
        let status = to_status(EngineError::SearchError("test".into()));
        assert_eq!(status.code(), tonic::Code::Internal);
    }

    #[test]
    fn error_mapping_index_not_found() {
        let status = to_status(EngineError::IndexError("repo-1".into()));
        assert_eq!(status.code(), tonic::Code::NotFound);
    }

    #[test]
    fn error_mapping_connection() {
        let status = to_status(EngineError::ConnectionError("refused".into()));
        assert_eq!(status.code(), tonic::Code::Unavailable);
    }

    #[test]
    fn error_mapping_timeout() {
        let status = to_status(EngineError::TimeoutError("30s".into()));
        assert_eq!(status.code(), tonic::Code::DeadlineExceeded);
    }

    #[test]
    fn error_mapping_rate_limited() {
        let status = to_status(EngineError::RateLimited(5));
        assert_eq!(status.code(), tonic::Code::ResourceExhausted);
    }

    #[test]
    fn error_mapping_conflict() {
        let status = to_status(EngineError::ConflictError {
            path: "src/main.rs".into(),
            details: "overlap".into(),
        });
        assert_eq!(status.code(), tonic::Code::Aborted);
    }
}
