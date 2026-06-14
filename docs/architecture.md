# UltimateCoders Architecture

## System Overview

UltimateCoders is a distributed AI coding system where multiple coding agents collaborate on software tasks using an Orchestrator-Worker pattern. The system provides shared layered memory and hybrid code retrieval (Text + Semantic + AST) across multiple Git repositories.

The architecture follows a Rust-core / Python-agent split:

- **Rust core** handles performance-critical work: indexing, search, memory storage, scheduling, fault tolerance.
- **Python agent layer** handles LLM interaction: task decomposition, code generation, tool invocation.
- **Bridge** between them uses PyO3 FFI (local, single-machine) or gRPC (remote, distributed), switchable at runtime.

```
+-------------------+     +-------------------+
|   Python Agent    |     |   Python Agent    |
|   Orchestrator    |     |     Worker        |
+--------+----------+     +--------+----------+
         |                         |
         |  Engine API (PyO3/gRPC) |
         |                         |
+--------v-------------------------v----------+
|              Rust Core Engine               |
|  +----------+ +--------+ +---------+       |
|  | Indexer  | | Search | | Memory  |       |
|  +----------+ +--------+ +---------+       |
|  +----------+ +--------+ +---------+       |
|  |Scheduler | |Checkpoint| |Conflict|      |
|  +----------+ +--------+ +---------+       |
+--------+----------+---------+---------+-----+
         |          |         |         |
    +----v---+ +----v---+ +--v----+ +--v----+
    |  TiKV  | | Qdrant | | PgSQL | | NATS  |
    +--------+ +--------+ +-------+ +-------+
```

---

## Rust Crates

### uc-types

Shared type definitions and the `EngineApi` trait. Zero I/O, zero framework dependencies.

Key types:

| Type | Purpose |
|------|---------|
| `EngineApi` trait | Unified contract for all engine operations (search, index, memory, health) |
| `EngineError` | Error enum mapped to both PyO3 exceptions and gRPC status codes |
| `SearchQuery` / `SearchResult` | Hybrid search request/response (Text, Semantic, AST, Hybrid modes) |
| `MemoryKey` / `MemoryEntry` | Layered memory keys (Task, Project, Global) and entries |
| `Task` / `Subtask` / `AgentEvent` | Orchestration types for task decomposition and event sourcing |

`EngineApi` trait signature:

```rust
#[async_trait]
pub trait EngineApi: Send + Sync {
    async fn search(&self, query: SearchQuery) -> Result<SearchResult, EngineError>;
    async fn index_repo(&self, request: IndexRequest) -> Result<IndexResponse, EngineError>;
    async fn get_index_state(&self, repo_id: &str) -> Result<RepoIndexState, EngineError>;
    async fn remove_index(&self, repo_id: &str) -> Result<(), EngineError>;
    async fn read_memory(&self, request: MemoryReadRequest) -> Result<Option<MemoryEntry>, EngineError>;
    async fn write_memory(&self, request: MemoryWriteRequest) -> Result<MemoryEntry, EngineError>;
    async fn delete_memory(&self, key: &MemoryKey) -> Result<(), EngineError>;
    async fn search_memory(&self, request: MemorySearchRequest) -> Result<MemorySearchResponse, EngineError>;
    async fn health(&self) -> Result<HealthStatus, EngineError>;
}
```

### uc-engine

Core engine implementing `EngineApi` via `LocalEngine`. Contains all business logic.

Modules:

| Module | Description |
|--------|-------------|
| `local` | `LocalEngine` -- in-process `EngineApi` implementation |
| `indexer/` | Text (trigram/ngram), Semantic (embedding + Qdrant), AST (tree-sitter) indexers |
| `memory/` | Short-term (TiKV) and long-term (Qdrant) memory stores |
| `search/` | Hybrid search engine combining text, semantic, and AST results |
| `scheduler/` | Orchestrator for task decomposition and worker assignment |
| `git/` | Repository management, diff computation for incremental indexing |
| `config` | Engine configuration from environment variables |
| `metadata/` | PostgreSQL metadata store (symbols, references, repo state) |
| `events` | Event sourcing store and task snapshot types |
| `checkpoint` | Checkpoint manager for snapshot + event replay recovery |
| `conflict/` | Intent-based conflict detection and three-way merge |
| `rate_limiter` | Dual-dimension (RPM + TPM) token bucket rate limiter |
| `circuit_breaker` | Circuit breaker for LLM API fault tolerance |

Feature flags:

- `storage` -- enables real TiKV, Qdrant, PostgreSQL clients (off = in-memory fallbacks)
- `indexing` -- enables file-system indexing and tree-sitter AST parsing

### uc-grpc

gRPC server and client. The server wraps any `EngineApi` implementor as a tonic service. The client implements `EngineApi` by calling a remote gRPC server.

```
+----------------+         +------------------+
|  GrpcServer    |<--------|  LocalEngine     |
|  (tonic)       | wraps   |  (EngineApi)     |
+----------------+         +------------------+

+----------------+         +------------------+
| GrpcEngineClient|-------->|  Remote gRPC     |
|  (EngineApi)   | calls   |  Server          |
+----------------+         +------------------+
```

The `conversions` module handles bidirectional type mapping between protobuf generated types and `uc-types`.

### uc-grpc-server

Standalone binary crate. Starts a tonic gRPC server backed by `LocalEngine`.

```bash
# Start the server
cargo run -p uc-grpc-server

# With custom address
UC_GRPC_ADDR="[::]:9090" cargo run -p uc-grpc-server
```

### uc-python

PyO3 binding that exposes a single `PyEngine` class to Python. The class switches between `LocalEngine` (PyO3 FFI, in-process) and `GrpcEngineClient` (tonic, remote) at construction time.

Key design points:

- Sync methods release the GIL via `py.allow_threads()` during Rust computation
- Async methods use `pyo3-async-runtimes` to return Python coroutines
- Rust futures crossing the FFI boundary are `Send + 'static` (no borrowed Python references)

---

## Python Agent Layer

### Engine Factory

`ultimate_coders.engine.Engine` wraps the Rust `PyEngine` and provides a Pythonic API:

```python
from ultimate_coders.engine import create_engine

# Local mode (single machine, PyO3 FFI)
engine = create_engine(mode="local")

# Remote mode (cluster, gRPC)
engine = create_engine(mode="grpc", grpc_endpoint="http://localhost:50051")
```

### Orchestrator

`ultimate_coders.agent.orchestrator.Orchestrator` receives a user task, uses an LLM to decompose it into subtasks with a dependency DAG, assigns subtasks to workers, and aggregates results in topological order.

Communication channels (via NATS):

| Subject | Purpose |
|---------|---------|
| `task.assign.{worker_id}` | Push subtask assignment to a specific worker |
| `task.result.{task_id}` | Worker reports subtask result |
| `agent.events` | All agent actions/observations (event sourcing) |
| `agent.checkpoints` | Checkpoint creation events |
| `agent.heartbeat.{worker_id}` | Worker heartbeat for liveness monitoring |

### Worker

`ultimate_coders.agent.worker.Worker` executes subtasks by calling LLM APIs and tools, then reports results. Workers register their capabilities (languages, frameworks) with the orchestrator.

### LLM Client

`ultimate_coders.agent.llm.LlmClient` wraps the Anthropic API with:

- Exponential backoff + jitter retry on rate limits
- Token bucket rate limiting (RPM + TPM dual dimension)
- Model fallback chain: Opus -> Sonnet -> Haiku
- Circuit breaker for cascading failure protection

### Configuration

`ultimate_coders.config` loads configuration from environment variables with sensible defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `UC_ENGINE_MODE` | `local` | Engine mode (`local` or `grpc`) |
| `UC_GRPC_ENDPOINT` | - | gRPC server endpoint (required for grpc mode) |
| `UC_TIKV_PD_ENDPOINTS` | `127.0.0.1:2379` | TiKV PD endpoints |
| `UC_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant URL |
| `UC_POSTGRES_URL` | `postgresql://localhost:5432/ultimatecoders` | PostgreSQL URL |
| `UC_NATS_URL` | `nats://127.0.0.1:4222` | NATS URL |
| `ANTHROPIC_API_KEY` | - | Anthropic API key |

---

## Storage Layer

### TiKV -- Short-Term Memory

Stores task-scoped, volatile data:

- Task context (code diffs, decision records, progress state)
- Task state snapshots (for checkpoint recovery)
- File locks (intent-based conflict detection)
- Cache entries

Access pattern: fast KV read/write by `MemoryKey::Task { task_id, key }`.

### Qdrant -- Long-Term Memory + Semantic Index

Stores persistent, semantically searchable data:

- Project-level knowledge embeddings (architecture understanding, decision history)
- Code chunk embeddings (AST-aware chunking -> Voyage Code 3 embedding)
- Long-term memory entries with vector similarity search

Collection design:

- Collection: `code_embeddings`
- Vector size: 1024 (Voyage Code 3)
- Distance: Cosine
- Payload indexes on `repo_id`, `language`, `chunk_type`, `symbol_kind`

### PostgreSQL -- Structured Metadata

Stores relational data for structured queries:

| Table | Purpose |
|-------|---------|
| `repos` | Registered repositories, index state |
| `symbols` | Extracted symbol definitions (function, class, type, etc.) |
| `references` | Symbol references (call sites, imports, type usages) |
| `agents` | Worker registration and capabilities |
| `tasks` | Task definitions and subtask state |
| `index_state` | Per-repo index state (last indexed SHA, file counts) |

---

## Communication

### gRPC (Synchronous)

Used for request-response operations where the caller needs an immediate result:

- Task assignment and status queries
- Memory read/write operations
- Search queries
- Index operations

Proto definitions mirror the `EngineApi` trait. The `uc-grpc` crate handles type conversions between proto and `uc-types`.

### NATS JetStream (Asynchronous)

Used for event streaming where producers and consumers are decoupled:

- Agent events (event sourcing for checkpoint/recovery)
- Index update notifications (webhook/polling triggers)
- Worker heartbeats (liveness monitoring)
- Checkpoint creation events
- Edit intent broadcasts (conflict detection)

JetStream provides:

- Durable streams (events persist across restarts)
- Replay from any offset (state reconstruction)
- Consumer groups (independent progress tracking)
- Exactly-once delivery semantics (with ack)

---

## Memory System

The memory system is layered, with a unified `MemoryStore` that routes reads and writes to the appropriate backend.

```
                    MemoryStore
                   /            \
                  /              \
    ShortTermMemory          LongTermMemory
    (TiKV-backed)            (Qdrant-backed)
    - Task-scoped            - Project-scoped
    - Volatile               - Persistent
    - Fast KV access         - Semantic search
```

Read flow:

1. `MemoryReadRequest` specifies a `MemoryKey` (Task, Project, or Global)
2. Task-scoped keys -> read from short-term (TiKV)
3. Project/Global keys -> read from long-term (Qdrant)
4. If `include_semantic=true`, also search long-term memory semantically

Write flow:

1. `MemoryWriteRequest` specifies key, content, and metadata
2. Task-scoped keys -> write to short-term (TiKV) with TTL
3. Project/Global keys -> write to long-term (Qdrant) with embedding
4. Memory event broadcast via NATS for cross-agent awareness

Content types:

| Type | Use Case |
|------|----------|
| `Text` | Decisions, notes, observations |
| `Structured` | JSON data (task state, configuration) |
| `Code` | Code snippets with language tag |
| `Diff` | File changes (unified diff) |
| `Reference` | Links to external resources |

---

## Search System

Hybrid search combines three retrieval modes with relevance-weighted scoring.

```
              SearchQuery
             /     |      \
            /      |       \
    Text Search  Semantic  AST Search
    (trigram/    (vector   (tree-sitter
     ngram)     similarity)  -> PostgreSQL)
            \      |       /
             \     |      /
           Result Fusion (score-weighted merge)
```

### Text Search

- Language-aware tokenizer: splits `camelCase` and `snake_case` identifiers into sub-tokens
- Trigram/ngram inverted index for substring matching
- Similar to Zoekt/ripgrep approach

### Semantic Search

- AST-aware chunking: each function/method/class is one chunk
- Embedding via Voyage Code 3 API (1024 dimensions, 32K context)
- Vector similarity search in Qdrant (cosine distance)
- Fallback: BLAKE3 hash-based deterministic embeddings for testing without API access

### AST Search

- tree-sitter parsing for 60+ languages
- Symbol definitions and references stored in PostgreSQL
- Structured queries: symbol search, call chains, implementations, imports

### Indexing Pipeline

```
Git Repo
    |
    v
Incremental? --yes--> git diff --> process changed files only
    |                       |
    no                      v
    |               Full walk of working tree
    v                       |
    v<----------------------+
    |
    v
For each file:
    1. Text indexer: tokenize + update inverted index
    2. AST indexer: tree-sitter parse -> extract symbols/references -> PostgreSQL
    3. Semantic indexer: AST-aware chunk -> embed -> upsert to Qdrant
    |
    v
Update index_state in PostgreSQL (last_indexed_sha)
```

Index consistency:

| Trigger | Latency | Use Case |
|---------|---------|----------|
| Webhook (push event) | Seconds | Primary for GitHub/GitLab repos |
| Polling (scheduled) | Minutes | Fallback, repos without webhooks |
| Manual API call | On-demand | Force reindex, schema migration |
| Periodic audit | Hourly/daily | Catch missed events |

---

## Fault Tolerance

### Event Sourcing + Checkpoint Recovery

All agent actions and observations are appended to an event stream (NATS JetStream). Periodically, a complete state snapshot is stored in TiKV.

Recovery flow:

1. Load latest snapshot from TiKV
2. Replay events from NATS JetStream after the snapshot's event offset
3. Re-assign incomplete subtasks to available workers
4. Resume orchestration from reconstructed state

```rust
// CheckpointManager API
async fn record_event(&self, subject: &str, event: AgentEventType) -> Result<u64>;
async fn create_snapshot(&self, task_id: &str) -> Result<String>;
async fn recover(&self, task_id: &str) -> Result<TaskSnapshot>;
```

### Conflict Detection and Resolution

Intent-based locking: workers declare edit intents (file path + line ranges) before modifying files. The `ConflictDetector` checks for overlapping edits.

Resolution pipeline (tiered):

| Tier | Strategy | Success Rate |
|------|----------|-------------|
| 1 | Auto-merge (three-way diff) | ~70% |
| 2 | LLM-assisted merge | ~90% |
| 3 | Re-assign to single worker with full context | ~98% |
| 4 | Human escalation | 100% (manual) |

```rust
// ConflictDetector API
fn declare_intent(&self, intent: EditIntent) -> ConflictResult;
fn check_conflict(&self, file_path: &str, worker_id: &str, regions: &[LineRange]) -> ConflictResult;

// Three-way merge
fn three_way_merge(base: &str, ours: &str, theirs: &str) -> MergeResult;
```

### LLM API Rate Limiting

Dual-dimension token bucket (RPM + TPM) with priority queuing and model fallback.

```rust
// LlmRateLimiter API
fn try_acquire(&self, estimated_tokens: f64) -> Result<(), EngineError>;
fn release(&self);
fn rpm_available(&self) -> f64;
fn tpm_available(&self) -> f64;
```

Model fallback chain: `Opus -> Sonnet -> Haiku` based on task complexity and rate limit availability.

### Circuit Breaker

Protects against cascading LLM API failures.

States: `Closed` (healthy) -> `Open` (failing, reject requests) -> `HalfOpen` (probe with one request).

```rust
// CircuitBreaker API
fn allow_request(&self) -> Result<(), EngineError>;
fn record_success(&self);
fn record_failure(&self);
fn state(&self) -> CircuitState;
```

---

## Task Scheduling & Night-time Orchestration

### Overview

UltimateCoders 支持任务调度能力，允许在夜间低负载时段自动执行批量任务（如代码审查、索引重建、知识库整理等），最大化资源利用率。

### Architecture

```
[YAML Config] ──load──> [Python Scheduler API] ──PyO3──> [Rust SchedulerService]
[User API call] ───────> [Python Scheduler API] ──PyO3──> [Rust SchedulerService]
                                                              |
                                                      [tokio-cron-scheduler]
                                                              |
                                                    [NightWindow Guard]
                                                              |
                                                    [ScheduleDispatcher]
                                                       /            \
                                          [OrchestratorDispatcher]  [LoggingDispatcher]
                                               (NATS publish)       (log only)
                                                       |
                                              [NATS: schedule.trigger.{id}]
                                                       |
                                              [Python Orchestrator]
                                                       |
                                              [Orchestrator.submit_task()]
                                                       |
                                              [Worker execution]
                                                       |
                                              [PostgreSQL: execution_history]
```

### Supported Task Types

| Type | Trigger | Definition | Example |
|------|---------|-----------|---------|
| System maintenance (cron) | Cron expression | YAML config file, loaded at startup | "每天 22:00 重建索引" |
| User deferred (one-shot) | execute_after timestamp | Python API | "今晚执行此审查" |

### Night-Window Guard

调度器内置 Night-Window Guard 逻辑：当任务触发时，检查当前时间是否在配置的夜间窗口内。如果不在窗口内，任务被延迟到下一个窗口起始时间。

- 支持跨午夜窗口（如 `22:00-06:00`）
- 时区感知（基于 IANA 时区名，如 `Asia/Shanghai`）
- 窗口配置存储在 PostgreSQL，支持运行时修改

```rust
// NightWindow API
fn is_within_window(&self, now: DateTime<Tz>) -> bool;
fn next_window_start(&self, now: DateTime<Tz>) -> DateTime<Tz>;
fn next_window_end(&self, now: DateTime<Tz>) -> DateTime<Tz>;
```

### Night-Window Exclusive Mode

夜间窗口激活时，调度任务独占 Worker 资源：

1. Orchestrator 维护 `night_window_active` 状态标志
2. 当 `night_window_active=True` 时，实时任务（非调度任务）进入 `_pending_tasks` 队列
3. 调度任务（带 `_scheduled` 标记）绕过队列，立即执行
4. 夜间窗口关闭时，调用 `flush_pending_tasks()` 执行所有排队的实时任务
5. 窗口状态通过 NATS 事件 `schedule.window.opened` / `schedule.window.closed` 传递

```python
# Orchestrator night-window integration
orchestrator.set_night_window_active(True)   # 窗口打开
orchestrator.submit_task("实时任务")           # 排队等待
orchestrator.submit_task("调度任务", _scheduled=True)  # 立即执行
orchestrator.set_night_window_active(False)   # 窗口关闭
await orchestrator.flush_pending_tasks()      # 执行排队的实时任务
```

### ScheduleDispatcher

调度触发后，通过 `ScheduleDispatcher` trait 分发任务：

| 实现 | 说明 | Feature Gate |
|------|------|-------------|
| `OrchestratorDispatcher` | 通过 NATS 发布 `schedule.trigger.{task_id}` 消息 | `messaging` |
| `LoggingDispatcher` | 仅记录日志（测试/无 NATS 环境） | 无 |

当 NATS 不可用时，`OrchestratorDispatcher` 优雅降级为日志记录，不会导致调度失败。

### Configuration

#### YAML 配置文件（系统维护任务）

```yaml
# config/scheduled_tasks.yaml
night_window:
  start: "22:00"
  end: "06:00"
  timezone: "Asia/Shanghai"

tasks:
  - description: "Rebuild search index for project-alpha"
    cron_expression: "0 22 * * *"
    project_id: "project-alpha"

  - description: "Run code review for project-beta"
    cron_expression: "0 23 * * 1-5"
    project_id: "project-beta"
```

#### Python API（用户延迟任务）

```python
from ultimate_coders.agent.scheduler import Scheduler
from ultimate_coders.agent.orchestrator import Orchestrator

scheduler = Scheduler()
scheduler.set_night_window("22:00", "06:00", "Asia/Shanghai")

# Cron 任务
scheduler.create_cron_job("每日索引重建", "0 22 * * *", project_id="my-project")

# 一次性延迟任务
scheduler.create_one_shot_job("代码审查", "2024-01-15T22:00:00Z", project_id="my-project")

# Orchestrator 集成
orchestrator = Orchestrator(scheduler=scheduler)
orchestrator.schedule_task("每周审查", cron="0 3 * * 1", project_id="my-project")
```

### Persistence and Recovery

- 调度配置持久化到 PostgreSQL（`scheduled_tasks` 表）
- 执行历史持久化到 PostgreSQL（`execution_history` 表）
- 系统重启后自动加载调度配置并恢复调度
- `tokio-cron-scheduler` 在 `scheduler` feature 启用时提供运行时调度

### Feature Gates

| Feature | 说明 | 依赖 |
|---------|------|------|
| `scheduler` | 启用 tokio-cron-scheduler 运行时 | `tokio-cron-scheduler` |
| `messaging` | 启用 NATS 分发（OrchestratorDispatcher） | `async-nats` |
| `storage` | 启用 PostgreSQL 持久化（PostgresScheduleStore） | `sqlx` |

无 feature 启用时，调度器仍可工作（InMemoryStore + LoggingDispatcher），适合测试和开发环境。

---

## Bridge Layer: PyO3 FFI + gRPC Dual-Mode

The `Engine` class in the Python ergonomic layer switches between local and remote at construction time:

```python
# Local mode: Rust runs in-process via PyO3 FFI
engine = Engine(mode="local")

# Remote mode: Rust runs in a separate gRPC server
engine = Engine(mode="grpc", grpc_endpoint="http://localhost:50051")
```

Both modes implement the same `EngineApi` trait in Rust, ensuring identical method signatures at compile time. The Python layer adds cross-cutting concerns (logging, metrics) on top.

Key design decisions:

1. **Error normalization**: Both PyO3 and gRPC paths map `EngineError` variants to the same Python exception types
2. **Type consistency**: Proto message types and PyO3 `#[pyclass]` types share the same `uc-types` definitions
3. **GIL handling**: Sync methods use `py.allow_threads()` to release the GIL; async methods use `pyo3-async-runtimes`
4. **Future constraints**: Rust futures crossing FFI must be `Send + 'static` -- no borrowed Python references inside futures

---

## Code Execution Security

MVP: local direct execution (fast iteration).

Architecture预留: `Sandbox` trait for future isolation:

```rust
trait Sandbox: Send + Sync {
    fn execute(&self, command: &str, context: &ExecutionContext) -> Result<ExecutionResult, EngineError>;
    fn is_filesystem_isolated(&self) -> bool;
    fn is_network_isolated(&self) -> bool;
    fn resource_limits(&self) -> ResourceLimits;
}
```

Production: Docker/gVisor container sandbox implementing the `Sandbox` trait.

---

## Sandbox Mode

### Overview

Sandbox mode is a deployment configuration where both the Orchestrator (task decomposition) and the Worker (subtask execution) use the `claude` CLI as the unified execution engine. No Python `LLMClient` is required -- the entire system runs with just the `claude` CLI and an `ANTHROPIC_API_KEY`.

This mode is the recommended way to run UltimateCoders locally for development and testing. It requires zero external infrastructure (TiKV, Qdrant, PostgreSQL, NATS) thanks to in-memory fallbacks.

### Architecture

```
User Task
    |
    v
Orchestrator (sandbox_manager set, llm_client=None)
    |
    | decompose_task() ── DecomposeAdapter ──> claude -p "decompose..." --max-turns 1
    |                                              |
    |                                         Claude Code CLI
    |                                              |
    |                                         JSON subtask list
    |                                              |
    v                                         parse_decomposition_output()
Subtask[]  <───────────────────────────────────────┘
    |
    v
Worker (execution_mode="sandbox")
    |
    | _execute_in_sandbox() ── SandboxManager ──> ClaudeCodeAdapter ──> claude -p "<subtask>" --max-turns 20
    |                                                    |
    |                                               Claude Code CLI
    |                                                    |
    |                                               JSON AgentOutput
    |                                                    |
    v                                               parse_output()
SubtaskResult  <─────────────────────────────────────────┘
```

### DecomposeAdapter

Defined in `python/ultimate_coders/agent/sandbox.py`, `DecomposeAdapter` is an `AgentAdapter` specialized for task decomposition. Unlike `ClaudeCodeAdapter` (which executes coding subtasks with many turns), it is optimized for single-turn, strict JSON output.

**Command construction:**

```
claude -p "<decomposition prompt>" --output-format json --max-turns 1 --dangerously-skip-permissions
```

**Output parsing** (`parse_decomposition_output()`):

1. Extracts the text content from the Claude Code JSON response envelope (`{"result": "..."}`)
2. Strips markdown code fences if present
3. Parses the inner JSON array of subtask objects: `[{description, depends_on, file_constraints, expected_output}]`
4. Returns `list[dict]` which the Orchestrator converts to `list[Subtask]`

**Orchestrator integration** (`orchestrator.py`):

When `sandbox_manager is not None and llm_client is None`, `decompose_task()` takes the sandbox path:

1. Builds the shared decomposition prompt (system + user message)
2. Combines them into a single prompt for `claude -p`
3. Uses `DecomposeAdapter.build_request()` to construct the CLI invocation
4. Executes via `SandboxManager._execute_subprocess()`
5. Parses the result through `DecomposeAdapter.parse_output()` and `parse_decomposition_output()`
6. Converts the parsed items into `Subtask` objects via `_parse_decomposition_items()`

### ClaudeCodeAdapter

Defined in `python/ultimate_coders/agent/sandbox.py`, `ClaudeCodeAdapter` is the `AgentAdapter` used by Workers for subtask execution.

**Command construction:**

```
claude -p "<subtask prompt>" --output-format json --max-turns 20 --dangerously-skip-permissions
```

**Output parsing** (`parse_output()`):

1. Handles timeout and non-zero exit codes as failures
2. Parses the JSON output, extracting the summary from:
   - `result` key (top-level string)
   - `message` key
   - `messages` array (last assistant message)
3. Extracts token usage from the `usage` key (`input_tokens`, `output_tokens`, `total_cost_usd`)
4. Returns an `AgentOutput` with summary, token usage, and success status

### SandboxManager

`SandboxManager` orchestrates sandbox creation, execution, and cleanup. It maintains a pool of `SandboxHandle` instances and delegates command construction/parsing to the configured `AgentAdapter`.

Key methods:

| Method | Description |
|--------|-------------|
| `acquire()` | Get a sandbox handle from the pool or create a new one |
| `execute(prompt, working_dir)` | Execute a prompt via the adapter, return `AgentOutput` |
| `release(handle)` | Return a sandbox handle to the pool |
| `warm_up()` | Pre-warm sandbox instances for fast allocation |

When `engine` is `None` (pure Python mode), `SandboxManager._execute_subprocess()` runs the CLI command as an `asyncio.create_subprocess_exec()` call with the configured environment variables and timeout.

### SandboxConfig

Configuration dataclass controlling sandbox behavior:

| Field | Default | Description |
|-------|---------|-------------|
| `agent` | `"claude-code"` | Agent adapter to use |
| `backend` | `"subprocess"` | Isolation backend (`"subprocess"` or `"docker"`) |
| `project_path` | `""` | Path to the project directory |
| `api_key` | `None` | API key (falls back to `ANTHROPIC_API_KEY` env var) |
| `max_cpu_seconds` | `3600` | Maximum CPU time per execution |
| `max_memory_mb` | `8192` | Maximum memory in MB |
| `max_output_bytes` | `50 MB` | Maximum output size |
| `network` | `"full"` | Network access mode (`none`, `restricted`, `full`) |
| `warm_pool_size` | `2` | Pre-warmed sandbox instances |
| `env_vars` | `{}` | Additional environment variables |

### Backend Options

| Backend | Isolation | Use Case | Flag |
|---------|-----------|----------|------|
| `subprocess` | Process-level (shared filesystem) | Development, local testing | Default, or `--backend subprocess` |
| `docker` | Container-level (isolated filesystem + network) | Production, strong isolation | `--backend docker` |

The subprocess backend requires no additional setup. The Docker backend requires a pre-built sandbox image but provides filesystem and network isolation.

### Entry Point: `scripts/run_sandbox.py`

The sandbox mode entry script supports three operating modes:

#### CLI Mode (default)

```bash
python scripts/run_sandbox.py "Fix the bug in main.rs"
```

Submits a task, prints the decomposition (subtask list with dependencies), executes subtasks via Claude Code, and prints the final result. The auto-execute loop polls every 2 seconds for pending subtasks whose dependencies are satisfied.

#### TUI Mode (`--tui`)

```bash
python scripts/run_sandbox.py --tui "Fix the bug in main.rs"
python scripts/run_sandbox.py --tui
```

Launches a Textual terminal interface with:

- **Left**: ChatLog -- real-time event stream (task submitted, subtask started/completed/failed, tool calls)
- **Right**: SubtaskTree -- hierarchical subtask progress with status indicators
- **Bottom**: TaskInput -- text input field for submitting new tasks
- **Status bar**: Worker ID, backend type, progress counter

The TUI creates an Orchestrator + Worker internally and subscribes to the `TaskEventEmitter` for real-time updates.

#### Dashboard Mode (`--dashboard`)

```bash
python scripts/run_sandbox.py --dashboard
```

Starts the FastAPI web Dashboard on `http://localhost:8080/dashboard/` with an auto-execute loop running in a background thread. The Dashboard provides the full monitoring UI (health, workers, tasks, scheduler, circuit breaker, event log) and interactive operations (task submit, pause/resume, circuit breaker reset).

#### Infrastructure Flag (`--with-infra`)

```bash
python scripts/run_sandbox.py --with-infra "Implement auth"
```

Starts Docker Compose storage infrastructure (TiKV, Qdrant, PostgreSQL, NATS) before launching the selected mode, and stops it on exit. Without this flag, the system uses in-memory fallbacks for all storage -- zero external dependencies.

### No-Infrastructure MVP

Sandbox mode is designed for zero-dependency local deployment:

| Component | With Infrastructure | Without (Default) |
|-----------|--------------------|--------------------|
| Short-term memory | TiKV | In-memory dict |
| Long-term memory | Qdrant | In-memory dict |
| Structured metadata | PostgreSQL | In-memory dict |
| Messaging | NATS JetStream | In-process asyncio |
| Engine health | Full 11-component status | Degraded/fallback status |

The `Orchestrator` is constructed with `engine=None, llm_client=None, sandbox_manager=...`, and the `Worker` uses `execution_mode="sandbox"`. All storage operations fall back to in-memory implementations. The only external requirement is the `claude` CLI and an `ANTHROPIC_API_KEY`.

### File Layout

```
python/ultimate_coders/agent/
├── sandbox.py              # SandboxManager, SandboxConfig, AgentAdapter,
│                           # DecomposeAdapter, ClaudeCodeAdapter, CodexAdapter,
│                           # parse_decomposition_output()
├── orchestrator.py         # decompose_task() sandbox path
├── worker.py               # _execute_in_sandbox() (already existed)
└── types.py                # Subtask, SubtaskStatus, WorkerInfo, etc.

python/ultimate_coders/tui/
├── __init__.py             # Public API: SandboxTUI
├── app.py                  # Textual App with event listener + auto-execute loop
└── widgets.py              # ChatLog, SubtaskTree, TaskInput, StatusBar, LogoHeader

scripts/
└── run_sandbox.py          # Entry point: CLI / TUI / Dashboard modes
```

---

## Dashboard

### Overview

UltimateCoders 提供嵌入 Orchestrator 的 Web Dashboard，实时监控集群运行状态。Dashboard 直接读取 Orchestrator 内存状态（workers、tasks、scheduler），通过 PyO3/Rust 获取引擎健康数据，使用 SSE 推送实时更新到浏览器。

### Architecture

```
Browser ──SSE──> FastAPI (/dashboard/api/stream)
              ──GET──> FastAPI (/dashboard/api/*)
                          │
                     Orchestrator (嵌入)
                          │
                ┌─────────┼──────────┐
                │         │          │
          Engine.health()  workers   Scheduler
          (PyO3/Rust)     tasks     jobs/history
```

### Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web Framework | FastAPI | REST API + SSE + Jinja2 rendering |
| Template Engine | Jinja2 | Server-side HTML rendering |
| CSS | Tailwind CSS CDN | Dark theme styling (no node/npm) |
| Real-time | SSE (sse-starlette) | Push updates every 5 seconds |
| Server | Uvicorn | ASGI server in background thread |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/dashboard/` | GET | Dashboard HTML page (Jinja2 template) |
| `/dashboard/api/health` | GET | Engine health JSON (11 components) |
| `/dashboard/api/workers` | GET | Worker list JSON (load, heartbeat) |
| `/dashboard/api/tasks` | GET | Task status JSON (status counts, pending) |
| `/dashboard/api/scheduler` | GET | Scheduler status JSON (jobs, history) |
| `/dashboard/api/circuit-breaker` | GET | Circuit breaker + rate limiter JSON |
| `/dashboard/api/stream` | GET | SSE event stream (full snapshot every 5s) |
| `/dashboard/api/events` | GET | Recent event log JSON (last 200 entries) |
| `/dashboard/api/tasks/{id}/pause` | POST | Pause a running task |
| `/dashboard/api/tasks/{id}/resume` | POST | Resume a paused task |
| `/dashboard/api/circuit-breaker/reset` | POST | Reset circuit breaker to closed |
| `/dashboard/api/scheduler/jobs/{id}/trigger` | POST | Manually trigger a scheduled job |
| `/dashboard/api/tasks/flush-pending` | POST | Flush night-window pending queue |

### Panels

1. **Engine Health** -- 11 component status indicators from `LocalEngine.health()`, overall status (ok/degraded/error), version, uptime
2. **Workers** -- Registered worker list with load bars, heartbeat staleness warnings
3. **Tasks** -- Active tasks with status badges, status counts, night-window pending count, click-to-expand subtask DAG (Mermaid.js), pause/resume buttons
4. **Scheduler** -- Running state, night window status, registered jobs with trigger buttons, recent execution history
5. **Circuit Breaker** -- State (Closed/Open/HalfOpen), failure count, total calls, rejected, reset button (visible when open/half_open)
6. **Rate Limiter** -- RPM available, TPM available, active requests
7. **Event Log** -- Recent operational events (task pause/resume, CB reset, scheduler trigger, flush), color-coded by type, newest first

### Startup

```python
from ultimate_coders.agent.orchestrator import Orchestrator

orch = Orchestrator(engine=engine, scheduler=scheduler)
orch.start_dashboard(host="0.0.0.0", port=8080)
# Dashboard available at http://localhost:8080/dashboard/
# ...
orch.stop_dashboard()
```

The dashboard runs in a daemon thread via Uvicorn, so it does not block the Orchestrator's main event loop.

### Fallback Mode

When infrastructure is unavailable, the dashboard degrades gracefully:

| Condition | Behavior |
|-----------|----------|
| No engine | Health panel shows "Unavailable" |
| Engine health() fails | Health panel shows error with message |
| No scheduler | Scheduler panel shows "Not Available" |
| Scheduler list_jobs() fails | Jobs list empty, no crash |
| Storage fallback (TiKV/Qdrant/PG) | Components show "fallback" status, overall "degraded" |

### File Layout

```
python/ultimate_coders/
├── dashboard/
│   ├── __init__.py          # Public API: DashboardApp
│   ├── app.py               # FastAPI app + routes + SSE + data collection
│   ├── templates/
│   │   └── index.html       # Jinja2 + Tailwind dark theme dashboard
│   └── static/
│       └── dashboard.js     # SSE client + DOM update logic
├── agent/
│   └── orchestrator.py      # start_dashboard() / stop_dashboard()
```

### Dependencies

Added to `pyproject.toml`:

- `fastapi>=0.100.0`
- `uvicorn>=0.20.0`
- `jinja2>=3.1.0`
- `sse-starlette>=1.6.0`

### Docker Integration

The `docker-compose.yml` includes an `orchestrator` service with port 8080 mapped for the dashboard. The dashboard is accessible at `http://localhost:8080/dashboard/`.