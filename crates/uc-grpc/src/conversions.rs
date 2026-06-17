//! Conversions between protobuf message types and uc-types.
//!
//! Bidirectional mapping so the server can accept proto requests, call EngineApi,
//! and return proto responses — while the client can do the reverse.

use uc_types::{
    ComponentHealth, HealthStatus, IndexRequest, IndexResponse, MemoryContent, MemoryEntry,
    MemoryId, MemoryKey, MemoryMetadata, MemoryReadRequest, MemorySearchRequest,
    MemorySearchResponse, MemorySearchResult, MemorySearchScope, MemoryWriteRequest,
    RepoIndexState, RepoSpec, SearchMode, SearchQuery, SearchResult, SearchResultItem, Subtask,
    SubtaskStatus, Task, TaskStatus,
};

// ── Import generated proto types ──────────────────────────

use crate::ultimate_coders::{
    ComponentHealthProto, DeleteMemoryRequest, GetIndexStateRequest, GetIndexStateResponse,
    HealthResponse, IndexRepoRequest, IndexRepoResponse, MemoryEntryProto, MemorySearchResultProto,
    ReadMemoryRequest, ReadMemoryResponse, RemoveIndexRequest, RepoIndexStateProto,
    SearchMemoryRequest, SearchMemoryResponse, SearchRequest, SearchResponse,
    SearchResultItem as ProtoSearchResultItem, SearchStreamRequest, SubtaskProto,
    TaskEvent as TaskEventProto, TaskProto, WriteMemoryRequest, WriteMemoryResponse,
};

// ── Search conversions ────────────────────────────────────

impl From<SearchRequest> for SearchQuery {
    fn from(req: SearchRequest) -> Self {
        Self {
            query: req.query,
            modes: req
                .modes
                .iter()
                .map(|m| match m.as_str() {
                    "text" => SearchMode::Text,
                    "semantic" => SearchMode::Semantic,
                    "ast" => SearchMode::Ast,
                    _ => SearchMode::Hybrid,
                })
                .collect(),
            repo_ids: req.repo_ids,
            languages: req.languages,
            path_patterns: req.path_patterns,
            max_results: req.max_results,
        }
    }
}

impl From<SearchResult> for SearchResponse {
    fn from(result: SearchResult) -> Self {
        Self {
            items: result.items.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<SearchResultItem> for ProtoSearchResultItem {
    fn from(item: SearchResultItem) -> Self {
        Self {
            repo_id: item.repo_id,
            file_path: item.file_path,
            start_line: item.start_line,
            end_line: item.end_line,
            content_snippet: item.content_snippet,
            match_type: match item.match_type {
                SearchMode::Text => "text".to_string(),
                SearchMode::Semantic => "semantic".to_string(),
                SearchMode::Ast => "ast".to_string(),
                SearchMode::Hybrid => "hybrid".to_string(),
            },
            score: item.score,
            symbol_name: item.symbol_name,
            symbol_kind: item.symbol_kind,
            parent_symbol: item.parent_symbol,
        }
    }
}

// ── Index conversions ─────────────────────────────────────

impl From<IndexRepoRequest> for IndexRequest {
    fn from(req: IndexRepoRequest) -> Self {
        Self {
            repo: RepoSpec {
                repo_id: req.repo_id,
                remote_url: req.remote_url,
                default_branch: req.default_branch,
                local_path: req.local_path,
            },
            force_full: req.force_full,
        }
    }
}

impl From<IndexResponse> for IndexRepoResponse {
    fn from(resp: IndexResponse) -> Self {
        Self {
            repo_id: resp.repo_id,
            files_indexed: resp.files_indexed,
            symbols_extracted: resp.symbols_extracted,
            chunks_embedded: resp.chunks_embedded,
            duration_ms: resp.duration_ms,
        }
    }
}

impl From<GetIndexStateRequest> for String {
    fn from(req: GetIndexStateRequest) -> Self {
        req.repo_id
    }
}

impl From<RepoIndexState> for GetIndexStateResponse {
    fn from(state: RepoIndexState) -> Self {
        Self {
            repo_id: state.repo_id,
            indexed: state.indexed,
            last_indexed_sha: state.last_indexed_sha,
            files_count: state.files_count,
            symbols_count: state.symbols_count,
            chunks_count: state.chunks_count,
        }
    }
}

impl From<RemoveIndexRequest> for String {
    fn from(req: RemoveIndexRequest) -> Self {
        req.repo_id
    }
}

// ── Memory conversions ────────────────────────────────────

/// Helper: reconstruct a MemoryKey from proto fields.
pub fn memory_key_from_proto(
    key_scope: &str,
    task_id: &str,
    project_id: &str,
    key: &str,
) -> Result<MemoryKey, String> {
    match key_scope {
        "task" => {
            if task_id.is_empty() {
                return Err("task_id is required for task-scoped memory".into());
            }
            Ok(MemoryKey::Task {
                task_id: task_id.to_string(),
                key: key.to_string(),
            })
        }
        "project" => {
            if project_id.is_empty() {
                return Err("project_id is required for project-scoped memory".into());
            }
            Ok(MemoryKey::Project {
                project_id: project_id.to_string(),
                key: key.to_string(),
            })
        }
        "global" => Ok(MemoryKey::Global {
            key: key.to_string(),
        }),
        _ => Err(format!(
            "Invalid key_scope: '{}'. Must be 'task', 'project', or 'global'",
            key_scope
        )),
    }
}

/// Helper: extract key_scope string from a MemoryKey.
pub fn memory_key_to_parts(key: &MemoryKey) -> (&str, &str, &str, &str) {
    match key {
        MemoryKey::Task { task_id, key } => ("task", task_id.as_str(), "", key.as_str()),
        MemoryKey::Project { project_id, key } => {
            ("project", "", project_id.as_str(), key.as_str())
        }
        MemoryKey::Global { key } => ("global", "", "", key.as_str()),
    }
}

impl From<ReadMemoryRequest> for MemoryReadRequest {
    fn from(req: ReadMemoryRequest) -> Self {
        // Best-effort conversion; the server should validate key_scope.
        let key = match req.key_scope.as_str() {
            "task" => MemoryKey::Task {
                task_id: req.task_id,
                key: req.key,
            },
            "project" => MemoryKey::Project {
                project_id: req.project_id,
                key: req.key,
            },
            _ => MemoryKey::Global { key: req.key },
        };
        Self {
            key,
            include_semantic: req.include_semantic,
        }
    }
}

impl From<Option<MemoryEntry>> for ReadMemoryResponse {
    fn from(entry: Option<MemoryEntry>) -> Self {
        Self {
            entry: entry.map(MemoryEntryProto::from),
        }
    }
}

impl From<WriteMemoryRequest> for MemoryWriteRequest {
    fn from(req: WriteMemoryRequest) -> Self {
        let key = match req.key_scope.as_str() {
            "task" => MemoryKey::Task {
                task_id: req.task_id,
                key: req.key,
            },
            "project" => MemoryKey::Project {
                project_id: req.project_id,
                key: req.key,
            },
            _ => MemoryKey::Global { key: req.key },
        };
        let content = match req.content_type.as_str() {
            "structured" => MemoryContent::Structured(
                serde_json::from_str(&req.content)
                    .unwrap_or(serde_json::Value::String(req.content)),
            ),
            "code" => MemoryContent::Code {
                language: req.language.unwrap_or_default(),
                code: req.content,
            },
            "diff" => MemoryContent::Diff {
                file_path: req.file_path.unwrap_or_default(),
                diff: req.content,
            },
            "reference" => MemoryContent::Reference {
                uri: req.uri.unwrap_or_default(),
                description: req.description.unwrap_or_default(),
            },
            _ => MemoryContent::Text(req.content),
        };
        Self {
            key,
            content,
            metadata: MemoryMetadata {
                source_agent: req.source_agent,
                importance: req.importance,
                tags: req.tags,
                embedding: None,
            },
        }
    }
}

impl From<MemoryEntry> for MemoryEntryProto {
    fn from(entry: MemoryEntry) -> Self {
        let (content_type, content, language, file_path, uri, description) = match entry.content {
            MemoryContent::Text(s) => ("text".to_string(), s, None, None, None, None),
            MemoryContent::Structured(v) => (
                "structured".to_string(),
                v.to_string(),
                None,
                None,
                None,
                None,
            ),
            MemoryContent::Code {
                language: lang,
                code,
            } => ("code".to_string(), code, Some(lang), None, None, None),
            MemoryContent::Diff {
                file_path: fp,
                diff,
            } => ("diff".to_string(), diff, None, Some(fp), None, None),
            MemoryContent::Reference {
                uri: u,
                description: d,
            } => (
                "reference".to_string(),
                String::new(),
                None,
                None,
                Some(u),
                Some(d),
            ),
        };
        let (key_scope, key_task_id, key_project_id, key) = memory_key_to_parts(&entry.key);
        Self {
            id: entry.id.0,
            content_type,
            content,
            source_agent: entry.metadata.source_agent,
            importance: entry.metadata.importance,
            tags: entry.metadata.tags,
            created_at: entry.created_at.timestamp(),
            updated_at: entry.updated_at.timestamp(),
            key_scope: key_scope.to_string(),
            key_task_id: key_task_id.to_string(),
            key_project_id: key_project_id.to_string(),
            key: key.to_string(),
            language,
            file_path,
            uri,
            description,
        }
    }
}

impl From<MemoryEntry> for WriteMemoryResponse {
    fn from(entry: MemoryEntry) -> Self {
        Self {
            entry: Some(MemoryEntryProto::from(entry)),
        }
    }
}

impl From<DeleteMemoryRequest> for MemoryKey {
    fn from(req: DeleteMemoryRequest) -> Self {
        match req.key_scope.as_str() {
            "task" => MemoryKey::Task {
                task_id: req.task_id,
                key: req.key,
            },
            "project" => MemoryKey::Project {
                project_id: req.project_id,
                key: req.key,
            },
            _ => MemoryKey::Global { key: req.key },
        }
    }
}

impl From<SearchMemoryRequest> for MemorySearchRequest {
    fn from(req: SearchMemoryRequest) -> Self {
        let scope = match req.scope_type.as_str() {
            "project" => MemorySearchScope::Project {
                project_id: req.project_id,
            },
            "global" => MemorySearchScope::Global,
            _ => MemorySearchScope::All,
        };
        Self {
            query: req.query,
            scope,
            max_results: req.max_results,
            min_score: req.min_score,
        }
    }
}

impl From<MemorySearchResponse> for SearchMemoryResponse {
    fn from(resp: MemorySearchResponse) -> Self {
        Self {
            results: resp.results.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<MemorySearchResult> for MemorySearchResultProto {
    fn from(r: MemorySearchResult) -> Self {
        Self {
            entry: Some(MemoryEntryProto::from(r.entry)),
            score: r.score,
        }
    }
}

// ── Health conversions ────────────────────────────────────

impl From<HealthStatus> for HealthResponse {
    fn from(status: HealthStatus) -> Self {
        Self {
            status: status.status,
            version: status.version,
            uptime_seconds: status.uptime_seconds,
            components: status.components.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<ComponentHealth> for ComponentHealthProto {
    fn from(c: ComponentHealth) -> Self {
        Self {
            name: c.name,
            status: c.status,
            details: c.details,
        }
    }
}

// ── Reverse conversions (for client) ──────────────────────

impl From<SearchResponse> for SearchResult {
    fn from(resp: SearchResponse) -> Self {
        Self {
            items: resp.items.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<ProtoSearchResultItem> for SearchResultItem {
    fn from(item: ProtoSearchResultItem) -> Self {
        Self {
            repo_id: item.repo_id,
            file_path: item.file_path,
            start_line: item.start_line,
            end_line: item.end_line,
            content_snippet: item.content_snippet,
            match_type: match item.match_type.as_str() {
                "text" => SearchMode::Text,
                "semantic" => SearchMode::Semantic,
                "ast" => SearchMode::Ast,
                _ => SearchMode::Hybrid,
            },
            score: item.score,
            symbol_name: item.symbol_name,
            symbol_kind: item.symbol_kind,
            parent_symbol: item.parent_symbol,
        }
    }
}

impl From<IndexRepoResponse> for IndexResponse {
    fn from(resp: IndexRepoResponse) -> Self {
        Self {
            repo_id: resp.repo_id,
            files_indexed: resp.files_indexed,
            symbols_extracted: resp.symbols_extracted,
            chunks_embedded: resp.chunks_embedded,
            duration_ms: resp.duration_ms,
        }
    }
}

impl From<GetIndexStateResponse> for RepoIndexState {
    fn from(resp: GetIndexStateResponse) -> Self {
        Self {
            repo_id: resp.repo_id,
            indexed: resp.indexed,
            last_indexed_sha: resp.last_indexed_sha,
            files_count: resp.files_count,
            symbols_count: resp.symbols_count,
            chunks_count: resp.chunks_count,
        }
    }
}

impl From<ReadMemoryResponse> for Option<MemoryEntry> {
    fn from(resp: ReadMemoryResponse) -> Self {
        resp.entry.map(Into::into)
    }
}

impl From<MemoryEntryProto> for MemoryEntry {
    fn from(proto: MemoryEntryProto) -> Self {
        let content = match proto.content_type.as_str() {
            "structured" => MemoryContent::Structured(
                serde_json::from_str(&proto.content)
                    .unwrap_or(serde_json::Value::String(proto.content)),
            ),
            "code" => MemoryContent::Code {
                language: proto.language.unwrap_or_default(),
                code: proto.content,
            },
            "diff" => MemoryContent::Diff {
                file_path: proto.file_path.unwrap_or_default(),
                diff: proto.content,
            },
            "reference" => MemoryContent::Reference {
                uri: proto.uri.unwrap_or_default(),
                description: proto.description.unwrap_or_default(),
            },
            _ => MemoryContent::Text(proto.content),
        };
        // Reconstruct key from proto fields
        let key = match proto.key_scope.as_str() {
            "task" => MemoryKey::Task {
                task_id: proto.key_task_id,
                key: proto.key,
            },
            "project" => MemoryKey::Project {
                project_id: proto.key_project_id,
                key: proto.key,
            },
            _ => MemoryKey::Global { key: proto.key },
        };
        Self {
            id: MemoryId(proto.id),
            key,
            content,
            metadata: MemoryMetadata {
                source_agent: proto.source_agent,
                importance: proto.importance,
                tags: proto.tags,
                embedding: None,
            },
            created_at: chrono::DateTime::from_timestamp(proto.created_at, 0)
                .unwrap_or_else(chrono::Utc::now),
            updated_at: chrono::DateTime::from_timestamp(proto.updated_at, 0)
                .unwrap_or_else(chrono::Utc::now),
        }
    }
}

impl From<WriteMemoryResponse> for MemoryEntry {
    fn from(resp: WriteMemoryResponse) -> Self {
        resp.entry
            .map(MemoryEntry::from)
            .unwrap_or_else(|| MemoryEntry {
                id: MemoryId::new(),
                key: MemoryKey::Global { key: String::new() },
                content: MemoryContent::Text(String::new()),
                metadata: MemoryMetadata {
                    source_agent: String::new(),
                    importance: 0.0,
                    tags: vec![],
                    embedding: None,
                },
                created_at: chrono::Utc::now(),
                updated_at: chrono::Utc::now(),
            })
    }
}

impl From<SearchMemoryResponse> for MemorySearchResponse {
    fn from(resp: SearchMemoryResponse) -> Self {
        Self {
            results: resp.results.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<MemorySearchResultProto> for MemorySearchResult {
    fn from(proto: MemorySearchResultProto) -> Self {
        Self {
            entry: proto
                .entry
                .map(MemoryEntry::from)
                .unwrap_or_else(|| MemoryEntry {
                    id: MemoryId::new(),
                    key: MemoryKey::Global { key: String::new() },
                    content: MemoryContent::Text(String::new()),
                    metadata: MemoryMetadata {
                        source_agent: String::new(),
                        importance: 0.0,
                        tags: vec![],
                        embedding: None,
                    },
                    created_at: chrono::Utc::now(),
                    updated_at: chrono::Utc::now(),
                }),
            score: proto.score,
        }
    }
}

impl From<HealthResponse> for HealthStatus {
    fn from(resp: HealthResponse) -> Self {
        Self {
            status: resp.status,
            version: resp.version,
            uptime_seconds: resp.uptime_seconds,
            components: resp.components.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<ComponentHealthProto> for ComponentHealth {
    fn from(proto: ComponentHealthProto) -> Self {
        Self {
            name: proto.name,
            status: proto.status,
            details: proto.details,
        }
    }
}

// ── RepoIndexState conversions ────────────────────────────

impl From<RepoIndexState> for RepoIndexStateProto {
    fn from(state: RepoIndexState) -> Self {
        Self {
            repo_id: state.repo_id,
            indexed: state.indexed,
            last_indexed_sha: state.last_indexed_sha,
            files_count: state.files_count,
            symbols_count: state.symbols_count,
            chunks_count: state.chunks_count,
        }
    }
}

impl From<RepoIndexStateProto> for RepoIndexState {
    fn from(proto: RepoIndexStateProto) -> Self {
        Self {
            repo_id: proto.repo_id,
            indexed: proto.indexed,
            last_indexed_sha: proto.last_indexed_sha,
            files_count: proto.files_count,
            symbols_count: proto.symbols_count,
            chunks_count: proto.chunks_count,
        }
    }
}

// ── Batch / List / Stream conversions ────────────────────

impl From<SearchStreamRequest> for SearchQuery {
    fn from(req: SearchStreamRequest) -> Self {
        Self {
            query: req.query,
            modes: req
                .modes
                .iter()
                .map(|m| match m.as_str() {
                    "text" => SearchMode::Text,
                    "semantic" => SearchMode::Semantic,
                    "ast" => SearchMode::Ast,
                    _ => SearchMode::Hybrid,
                })
                .collect(),
            repo_ids: req.repo_ids,
            languages: req.languages,
            path_patterns: req.path_patterns,
            max_results: req.max_results,
        }
    }
}

// ── Task conversions ───────────────────────────────────────

/// Convert TaskStatus to proto string representation.
pub fn task_status_to_proto(status: &TaskStatus) -> &'static str {
    match status {
        TaskStatus::Created => "Created",
        TaskStatus::Planning => "Planning",
        TaskStatus::InProgress => "InProgress",
        TaskStatus::Completed => "Completed",
        TaskStatus::Failed => "Failed",
        TaskStatus::Paused => "Paused",
    }
}

/// Convert SubtaskStatus to proto string representation.
pub fn subtask_status_to_proto(status: &SubtaskStatus) -> &'static str {
    match status {
        SubtaskStatus::Pending => "Pending",
        SubtaskStatus::Assigned => "Assigned",
        SubtaskStatus::InProgress => "InProgress",
        SubtaskStatus::Completed => "Completed",
        SubtaskStatus::Failed => "Failed",
        SubtaskStatus::Conflicted => "Conflicted",
    }
}

impl From<Task> for TaskProto {
    fn from(task: Task) -> Self {
        Self {
            id: task.id.0,
            description: task.description,
            status: task_status_to_proto(&task.status).to_string(),
            project_id: task.project_id,
            subtask_count: task.subtasks.len() as u32,
            created_at: task.created_at.timestamp(),
            updated_at: task.updated_at.timestamp(),
            subtasks: task.subtasks.into_iter().map(Into::into).collect(),
        }
    }
}

impl From<Subtask> for SubtaskProto {
    fn from(st: Subtask) -> Self {
        Self {
            id: st.id.0,
            description: st.description,
            status: subtask_status_to_proto(&st.status).to_string(),
            depends_on: st.depends_on.iter().map(|id| id.0.clone()).collect(),
            assigned_worker: st.assigned_worker.map(|w| w.0),
        }
    }
}

/// Convert an AgentEventType from uc-engine into a proto TaskEvent.
/// Used by the WatchTask streaming RPC.
impl From<uc_engine::AgentEventType> for TaskEventProto {
    fn from(event: uc_engine::AgentEventType) -> Self {
        let (event_type, task_id, subtask_id, data) = match event {
            uc_engine::AgentEventType::TaskCreated {
                task_id,
                description,
            } => (
                "task_submitted".to_string(),
                task_id.0,
                String::new(),
                vec![("description".to_string(), description)]
                    .into_iter()
                    .collect(),
            ),
            uc_engine::AgentEventType::SubtaskAssigned {
                task_id,
                subtask_id,
                worker_id,
            } => (
                "subtask_assigned".to_string(),
                task_id.0,
                subtask_id.0,
                vec![("worker_id".to_string(), worker_id.0)]
                    .into_iter()
                    .collect(),
            ),
            uc_engine::AgentEventType::SubtaskStarted {
                task_id,
                subtask_id,
                worker_id,
            } => (
                "subtask_started".to_string(),
                task_id.0,
                subtask_id.0,
                vec![("worker_id".to_string(), worker_id.0)]
                    .into_iter()
                    .collect(),
            ),
            uc_engine::AgentEventType::ToolInvoked {
                task_id,
                subtask_id,
                tool_name,
                tool_input,
            } => (
                "tool_call".to_string(),
                task_id.0,
                subtask_id.0,
                vec![
                    ("tool_name".to_string(), tool_name),
                    ("tool_input".to_string(), tool_input),
                ]
                .into_iter()
                .collect(),
            ),
            uc_engine::AgentEventType::ToolResult {
                task_id,
                subtask_id,
                tool_output,
                success,
            } => (
                "tool_result".to_string(),
                task_id.0,
                subtask_id.0,
                vec![
                    ("tool_output".to_string(), tool_output),
                    ("success".to_string(), success.to_string()),
                ]
                .into_iter()
                .collect(),
            ),
            uc_engine::AgentEventType::FileModified {
                task_id,
                subtask_id,
                file_path,
                diff,
            } => (
                "file_modified".to_string(),
                task_id.0,
                subtask_id.0,
                vec![
                    ("file_path".to_string(), file_path),
                    ("diff".to_string(), diff),
                ]
                .into_iter()
                .collect(),
            ),
            uc_engine::AgentEventType::SubtaskCompleted {
                task_id,
                subtask_id,
                summary,
                success,
            } => (
                "subtask_completed".to_string(),
                task_id.0,
                subtask_id.0,
                vec![
                    ("summary".to_string(), summary),
                    ("success".to_string(), success.to_string()),
                ]
                .into_iter()
                .collect(),
            ),
            uc_engine::AgentEventType::SubtaskFailed {
                task_id,
                subtask_id,
                error,
                recoverable,
            } => (
                "subtask_failed".to_string(),
                task_id.0,
                subtask_id.0,
                vec![
                    ("error".to_string(), error),
                    ("recoverable".to_string(), recoverable.to_string()),
                ]
                .into_iter()
                .collect(),
            ),
            uc_engine::AgentEventType::CheckpointCreated {
                task_id,
                snapshot_id,
                ..
            } => (
                "checkpoint_created".to_string(),
                task_id.0,
                String::new(),
                vec![("snapshot_id".to_string(), snapshot_id)]
                    .into_iter()
                    .collect(),
            ),
            uc_engine::AgentEventType::EditIntent {
                worker_id,
                file_path,
                edit_type,
                regions,
            } => (
                "edit_intent".to_string(),
                String::new(),
                String::new(),
                vec![
                    ("worker_id".to_string(), worker_id.0),
                    ("file_path".to_string(), file_path),
                    ("edit_type".to_string(), edit_type),
                    ("regions".to_string(), format!("{:?}", regions)),
                ]
                .into_iter()
                .collect(),
            ),
        };

        Self {
            timestamp: chrono::Utc::now().to_rfc3339(),
            r#type: event_type,
            task_id,
            subtask_id: if subtask_id.is_empty() {
                None
            } else {
                Some(subtask_id)
            },
            data,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_request_roundtrip() {
        let proto = SearchRequest {
            query: "database".to_string(),
            modes: vec!["text".to_string(), "semantic".to_string()],
            repo_ids: vec!["repo1".to_string()],
            languages: vec!["rust".to_string()],
            path_patterns: vec!["src/".to_string()],
            max_results: 10,
        };
        let query: SearchQuery = proto.into();
        assert_eq!(query.query, "database");
        assert_eq!(query.modes.len(), 2);
        assert_eq!(query.modes[0], SearchMode::Text);
        assert_eq!(query.modes[1], SearchMode::Semantic);
    }

    #[test]
    fn memory_key_from_proto_task() {
        let key = memory_key_from_proto("task", "t1", "", "decisions").unwrap();
        assert_eq!(
            key,
            MemoryKey::Task {
                task_id: "t1".to_string(),
                key: "decisions".to_string(),
            }
        );
    }

    #[test]
    fn memory_key_from_proto_project() {
        let key = memory_key_from_proto("project", "", "p1", "architecture").unwrap();
        assert_eq!(
            key,
            MemoryKey::Project {
                project_id: "p1".to_string(),
                key: "architecture".to_string(),
            }
        );
    }

    #[test]
    fn memory_key_from_proto_global() {
        let key = memory_key_from_proto("global", "", "", "conventions").unwrap();
        assert_eq!(
            key,
            MemoryKey::Global {
                key: "conventions".to_string(),
            }
        );
    }

    #[test]
    fn memory_key_from_proto_invalid() {
        let result = memory_key_from_proto("invalid", "", "", "key");
        assert!(result.is_err());
    }

    #[test]
    fn memory_key_to_parts_task() {
        let key = MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "k".to_string(),
        };
        let (scope, task_id, project_id, k) = memory_key_to_parts(&key);
        assert_eq!(scope, "task");
        assert_eq!(task_id, "t1");
        assert_eq!(project_id, "");
        assert_eq!(k, "k");
    }
}
