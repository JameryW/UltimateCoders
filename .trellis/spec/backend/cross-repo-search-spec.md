# Cross-Repo Search & Memory Sharing Spec

> Executable contracts for cross-repository code retrieval and project-scoped memory sharing across distributed Workers.

---

## 1. Scope / Trigger

- Trigger: Worker executes subtask and needs code context from other repos, or Workers need to share knowledge via project-scoped memory
- Cross-layer: Python Worker → gRPC Engine → Gateway HybridSearchEngine / MemoryStore → Qdrant / TiKV
- Requires code-spec depth because it defines Worker Engine routing, search context injection, memory scoping, and NATS dispatch payload changes

---

## 2. Signatures

### Python Worker Engine Routing

```python
# NatsWorker._init_components() — Worker mode Engine creation
if self._grpc_endpoint:
    self._engine = Engine(
        mode="grpc",
        grpc_endpoint=self._grpc_endpoint,
        fallback_mode="auto",  # gRPC first, local fallback
    )
else:
    self._engine = Engine(mode="local")
```

### SearchQuery Builder

```python
class SearchQuery:
    def in_all_repos(self, engine: object) -> SearchQuery:
        """Auto-populate repo_ids from engine.list_repos()."""
        # Calls engine.list_repos(), extracts .repo_id
        # On failure: repo_ids stays [] (searches all)

    def in_repos(self, repo_ids: list[str]) -> SearchQuery:
        """Manual repo scope."""
```

### Worker Search & Memory Methods

```python
class Worker:
    def search_across_repos(
        self, query: str, modes: list[str] | None = None, max_results: int = 20,
    ) -> list | None:
        """Hybrid cross-repo search via Engine (gRPC when available)."""

    def _build_search_context(self, subtask: Subtask) -> str | None:
        """Auto-inject search results into subtask context_block.
        Uses subtask.project_id for scope, or in_all_repos() if no project_id.
        Non-fatal: returns None on any failure."""

    def read_shared_memory(
        self, key: str, project_id: str = "",
    ) -> object | None:
        """Read project-scoped memory (shared across Workers via Gateway)."""

    def write_shared_memory(
        self, key: str, content: str, project_id: str = "",
        content_type: str = "text", importance: float = 0.7,
        tags: list[str] | None = None,
    ) -> object | None:
        """Write project-scoped memory. importance=0.7 > long-term threshold."""
```

### Subtask.project_id

```python
@dataclass
class Subtask:
    ...
    project_id: str = ""  # Project scope for search/memory
```

### Rust NATS Dispatch

```rust
pub struct NatsSubtaskExecute {
    ...
    #[serde(default)]
    pub project_id: String,  // NEW: project scope for search/memory
}
```

---

## 3. Contracts

### Request Fields

| Field | Type | Constraints | Source |
|-------|------|-------------|--------|
| `SearchQuery.repo_ids` | `list[str]` | Empty = search all repos | `in_all_repos()` or `in_repos()` |
| `Subtask.project_id` | `str` | Default `""` = no project scope | NATS dispatch payload |
| `NatsSubtaskExecute.project_id` | `String` | `#[serde(default)]` = backward compatible | Rust Gateway |
| `write_shared_memory.importance` | `float` | Default 0.7 > long_term_importance_threshold (0.7) | Worker |

### Response Fields

| Field | Type | Notes |
|-------|------|-------|
| `SearchResult.items` | `list[SearchResultItem]` | Each has `.repo_id`, `.file_path`, `.content_snippet` |
| `search_across_repos` return | `list \| None` | Extracts `.items` from SearchResult, None if engine unavailable |
| `_build_search_context` return | `str \| None` | Formatted markdown with code snippets, None on failure |

### Environment Keys

| Key | Required | Description |
|-----|----------|-------------|
| `UC_GRPC_ENDPOINT` | Optional | gRPC Gateway endpoint for Worker mode |

---

## 4. Validation & Error Matrix

| Condition | Error / Behavior |
|-----------|-----------------|
| `engine is None` | All search/memory methods return `None` |
| `engine.list_repos()` fails | `in_all_repos()` leaves `repo_ids = []` (searches all) |
| `engine.search()` fails | `_build_search_context()` returns `None` (non-fatal) |
| `SearchResult` has no `.items` | `search_across_repos()` falls back to treating result as list |
| `project_id` empty | `read/write_shared_memory` uses `key_scope="global"` |
| `project_id` set | `read/write_shared_memory` uses `key_scope="project"` |
| gRPC unavailable + `fallback_mode="auto"` | Engine auto-falls back to local mode |

---

## 5. Good/Base/Bad Cases

### Good: Worker with gRPC, project-scoped search

```python
# Worker mode with gRPC endpoint configured
w = Worker(engine=Engine(mode="grpc", grpc_endpoint="http://gateway:50051", fallback_mode="auto"))
st = Subtask(description="fix auth", project_id="my-backend")
context = w._build_search_context(st)  # Searches my-backend repo only
```

### Base: Worker without gRPC, no project_id

```python
# Worker mode without gRPC endpoint
w = Worker(engine=Engine(mode="local"))
st = Subtask(description="fix auth")
context = w._build_search_context(st)  # Searches all repos (if any indexed locally)
```

### Bad: Engine None, search returns None

```python
w = Worker(engine=None)
result = w.search_across_repos("auth")  # Returns None — no crash
```

---

## 6. Tests Required

| Test | Assertion Point |
|------|-----------------|
| `test_subtask_has_project_id` | `Subtask(project_id="x").project_id == "x"` |
| `test_subtask_project_id_default_empty` | `Subtask().project_id == ""` |
| `test_search_across_repos_no_engine` | Returns `None` when `engine=None` |
| `test_build_search_context_no_engine` | Returns `None` when `engine=None` |
| `test_build_search_context_no_description` | Returns `None` for empty description |
| `test_read_shared_memory_no_engine` | Returns `None` when `engine=None` |
| `test_write_shared_memory_no_engine` | Returns `None` when `engine=None` |
| `test_search_query_in_all_repos` | `repo_ids` populated from `engine.list_repos()` |
| `test_search_query_in_all_repos_failure` | `repo_ids = []` on engine failure |

---

## 7. Wrong vs Correct

### Wrong: Worker always uses local Engine

```python
# _init_components() — always local, no cross-repo access
self._engine = Engine(mode="local")
```

#### Correct: Worker uses gRPC when endpoint configured

```python
if self._grpc_endpoint:
    self._engine = Engine(mode="grpc", grpc_endpoint=self._grpc_endpoint, fallback_mode="auto")
else:
    self._engine = Engine(mode="local")
```

### Wrong: NatsSubtaskExecute with empty project_id

```rust
let execute = NatsSubtaskExecute {
    ...
    project_id: String::new(),  // Always empty — remote Worker can't scope search
};
```

#### Correct: Extract project_id from Task

```rust
let project_id = store.get_task(task_id).map(|t| t.project_id.clone()).unwrap_or_default();
let execute = NatsSubtaskExecute {
    ...
    project_id,
};
```

---

## Design Decisions

### Decision: Engine gRPC fallback_mode="auto"

**Context**: Worker needs Gateway's shared search index, but Gateway may be temporarily unavailable.

**Options**:
1. `fallback_mode="none"` — crash if gRPC fails
2. `fallback_mode="auto"` — auto-switch to local on gRPC failure, auto-recover

**Decision**: `fallback_mode="auto"` — graceful degradation is critical for distributed Workers.

### Decision: _build_search_context is non-fatal

**Context**: Search injection enriches subtask context but is not required for execution.

**Decision**: Any failure in `_build_search_context()` returns `None` and the subtask proceeds without search context. This prevents search infrastructure issues from blocking task execution.

### Decision: Engine MCP Server for sandbox agents

**Context**: Sandbox agents (Claude Code, Codex) need dynamic access to search and memory during execution, not just the pre-injected static context block.

**Decision**: A lightweight MCP stdio server (`engine_mcp.py`) wraps `Engine.search()`, `read_memory()`, `write_memory()` as MCP tools. Auto-registered in `Worker.__init__` via `SandboxConfig.mcp_configs`. This lets sandbox agents call `search_code`, `read_memory`, `write_memory` during execution.

### Decision: agent_config_json propagation in NATS dispatch

**Context**: Per-subtask agent config (tools, MCP, system prompt) was lost during NATS dispatch — `NatsSubtaskExecute.agent_config_json` was always `None`.

**Decision**: Both `publish_ready_subtasks` and `dispatch_ready_subtasks` now use `st.agent_config_json.clone()` instead of `None`. This ensures remote Workers receive the correct agent configuration.

### Decision: projectId persistence round-trip

**Context**: `TaskState.projectId` was lost during persistence (toPersisted/fromPersisted) and gRPC sync (upsertTask hardcoded `""`).

**Decision**: `PersistedTask.projectId` field added, `toPersisted()`/`fromPersisted()` propagate it, and `upsertTask` uses `task.projectId ?? ""`. This ensures project scope survives restart and gRPC sync.
