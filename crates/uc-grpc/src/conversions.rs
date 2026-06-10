//! Conversions between protobuf message types and uc-types.
//!
//! Bidirectional mapping so the server can accept proto requests, call EngineApi,
//! and return proto responses — while the client can do the reverse.

use uc_types::{
    ComponentHealth, HealthStatus, IndexRequest, IndexResponse, MemoryContent, MemoryEntry,
    MemoryId, MemoryKey, MemoryMetadata, MemoryReadRequest, MemorySearchRequest,
    MemorySearchResponse, MemorySearchResult, MemorySearchScope, MemoryWriteRequest,
    RepoIndexState, RepoSpec, SearchMode, SearchQuery, SearchResult, SearchResultItem,
};

// ── Import generated proto types ──────────────────────────

use crate::ultimate_coders::{
    ComponentHealthProto, DeleteMemoryRequest, GetIndexStateRequest, GetIndexStateResponse,
    HealthResponse, IndexRepoRequest, IndexRepoResponse, MemoryEntryProto,
    MemorySearchResultProto, ReadMemoryRequest, ReadMemoryResponse, RemoveIndexRequest,
    SearchMemoryRequest, SearchMemoryResponse, SearchRequest, SearchResponse,
    SearchResultItem as ProtoSearchResultItem, WriteMemoryRequest, WriteMemoryResponse,
};

// ── Search conversions ────────────────────────────────────

impl From<SearchRequest> for SearchQuery {
    fn from(req: SearchRequest) -> Self {
        Self {
            query: req.query,
            modes: req.modes.iter().map(|m| match m.as_str() {
                "text" => SearchMode::Text,
                "semantic" => SearchMode::Semantic,
                "ast" => SearchMode::Ast,
                _ => SearchMode::Hybrid,
            }).collect(),
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
        _ => Err(format!("Invalid key_scope: '{}'. Must be 'task', 'project', or 'global'", key_scope)),
    }
}

/// Helper: extract key_scope string from a MemoryKey.
pub fn memory_key_to_parts(key: &MemoryKey) -> (&str, &str, &str, &str) {
    match key {
        MemoryKey::Task { task_id, key } => ("task", task_id.as_str(), "", key.as_str()),
        MemoryKey::Project { project_id, key } => ("project", "", project_id.as_str(), key.as_str()),
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
            _ => MemoryKey::Global {
                key: req.key,
            },
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
            _ => MemoryKey::Global {
                key: req.key,
            },
        };
        let content = match req.content_type.as_str() {
            "structured" => MemoryContent::Structured(
                serde_json::from_str(&req.content).unwrap_or(serde_json::Value::String(req.content)),
            ),
            "code" => {
                let parts: Vec<&str> = req.content.splitn(2, ':').collect();
                if parts.len() == 2 && !parts[0].is_empty() {
                    MemoryContent::Code {
                        language: parts[0].to_string(),
                        code: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Code {
                        language: String::new(),
                        code: req.content,
                    }
                }
            }
            "diff" => {
                let parts: Vec<&str> = req.content.splitn(2, ':').collect();
                if parts.len() == 2 && !parts[0].is_empty() {
                    MemoryContent::Diff {
                        file_path: parts[0].to_string(),
                        diff: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Diff {
                        file_path: String::new(),
                        diff: req.content,
                    }
                }
            }
            "reference" => {
                let parts: Vec<&str> = req.content.splitn(2, ':').collect();
                if parts.len() == 2 && !parts[0].is_empty() {
                    MemoryContent::Reference {
                        uri: parts[0].to_string(),
                        description: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Reference {
                        uri: req.content,
                        description: String::new(),
                    }
                }
            }
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
        let (content_type, content) = match entry.content {
            MemoryContent::Text(s) => ("text".to_string(), s),
            MemoryContent::Structured(v) => ("structured".to_string(), v.to_string()),
            MemoryContent::Code { language, code } => {
                ("code".to_string(), format!("{}:{}", language, code))
            }
            MemoryContent::Diff { file_path, diff } => {
                ("diff".to_string(), format!("{}:{}", file_path, diff))
            }
            MemoryContent::Reference { uri, description } => {
                ("reference".to_string(), format!("{}:{}", uri, description))
            }
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
            _ => MemoryKey::Global {
                key: req.key,
            },
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
                serde_json::from_str(&proto.content).unwrap_or(serde_json::Value::String(proto.content)),
            ),
            "code" => {
                // Parse "language:code" format
                let parts: Vec<&str> = proto.content.splitn(2, ':').collect();
                if parts.len() == 2 {
                    MemoryContent::Code {
                        language: parts[0].to_string(),
                        code: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Code {
                        language: String::new(),
                        code: proto.content,
                    }
                }
            }
            "diff" => {
                let parts: Vec<&str> = proto.content.splitn(2, ':').collect();
                if parts.len() == 2 {
                    MemoryContent::Diff {
                        file_path: parts[0].to_string(),
                        diff: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Diff {
                        file_path: String::new(),
                        diff: proto.content,
                    }
                }
            }
            "reference" => {
                let parts: Vec<&str> = proto.content.splitn(2, ':').collect();
                if parts.len() == 2 {
                    MemoryContent::Reference {
                        uri: parts[0].to_string(),
                        description: parts[1].to_string(),
                    }
                } else {
                    MemoryContent::Reference {
                        uri: proto.content,
                        description: String::new(),
                    }
                }
            }
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
            _ => MemoryKey::Global {
                key: proto.key,
            },
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
            entry: proto.entry
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
        assert_eq!(key, MemoryKey::Task {
            task_id: "t1".to_string(),
            key: "decisions".to_string(),
        });
    }

    #[test]
    fn memory_key_from_proto_project() {
        let key = memory_key_from_proto("project", "", "p1", "architecture").unwrap();
        assert_eq!(key, MemoryKey::Project {
            project_id: "p1".to_string(),
            key: "architecture".to_string(),
        });
    }

    #[test]
    fn memory_key_from_proto_global() {
        let key = memory_key_from_proto("global", "", "", "conventions").unwrap();
        assert_eq!(key, MemoryKey::Global {
            key: "conventions".to_string(),
        });
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
