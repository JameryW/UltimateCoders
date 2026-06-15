# Research: gRPC Integration for TUI

- **Query**: Understand proto definitions, gRPC server/client, TUI mock data flow, and build setup for PR2 gRPC integration
- **Scope**: internal
- **Date**: 2026-06-15

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/proto/engine.proto` | Proto definition for EngineService (9 RPCs, all unary) |
| `crates/uc-grpc/src/lib.rs` | gRPC crate root; includes generated proto via `tonic::include_proto!` |
| `crates/uc-grpc/src/server.rs` | GrpcServer wrapping EngineApi as tonic service |
| `crates/uc-grpc/src/client.rs` | GrpcEngineClient implementing EngineApi via tonic client |
| `crates/uc-grpc/src/conversions.rs` | Bidirectional type mapping between proto and uc-types |
| `crates/uc-grpc/build.rs` | Build script: compiles `proto/engine.proto` with tonic-build |
| `crates/uc-grpc-server/src/main.rs` | Standalone gRPC server binary (listens on `[::]:50051` by default) |
| `crates/uc-types/src/engine.rs` | EngineApi trait definition (the unified contract) |
| `tui/src/components/App.tsx` | Root TUI component with mock data flow |
| `tui/src/hooks/useCursor.ts` | CJK/IME cursor positioning hook |
| `tui/package.json` | TUI dependencies (includes @grpc/grpc-js and @grpc/proto-loader) |
| `tui/build.mjs` | esbuild bundler config for TUI |
| `tui/src/index.tsx` | TUI entry point |
| `tui/tsconfig.json` | TypeScript config (ES2022, bundler module resolution) |

### Proto Definition Details

**Package**: `ultimate_coders`
**Service**: `EngineService` (9 unary RPCs, no streaming)

#### RPC Methods

| Method | Request | Response | Description |
|--------|---------|----------|-------------|
| `Search` | `SearchRequest` | `SearchResponse` | Hybrid search across indexed repos |
| `IndexRepo` | `IndexRepoRequest` | `IndexRepoResponse` | Index a repository |
| `GetIndexState` | `GetIndexStateRequest` | `GetIndexStateResponse` | Check repo index status |
| `RemoveIndex` | `RemoveIndexRequest` | `RemoveIndexResponse` | Remove a repo index |
| `ReadMemory` | `ReadMemoryRequest` | `ReadMemoryResponse` | Read a memory entry |
| `WriteMemory` | `WriteMemoryRequest` | `WriteMemoryResponse` | Write a memory entry |
| `DeleteMemory` | `DeleteMemoryRequest` | `DeleteMemoryResponse` | Delete a memory entry |
| `SearchMemory` | `SearchMemoryRequest` | `SearchMemoryResponse` | Semantic memory search |
| `Health` | `HealthRequest` | `HealthResponse` | Health check |

#### Key Message Types

**SearchRequest** fields: `query` (string), `modes` (repeated string: "text"/"semantic"/"ast"/"hybrid"), `repo_ids`, `languages`, `path_patterns`, `max_results` (uint32)

**SearchResultItem** fields: `repo_id`, `file_path`, `start_line`, `end_line`, `content_snippet`, `match_type`, `score` (float), optional `symbol_name`, `symbol_kind`, `parent_symbol`

**ReadMemoryRequest** fields: `key_scope` ("task"/"project"/"global"), `task_id`, `project_id`, `key`, `include_semantic`

**WriteMemoryRequest** fields: `key_scope`, `task_id`, `project_id`, `key`, `content_type` ("text"/"structured"/"code"/"diff"/"reference"), `content`, `source_agent`, `importance` (float), `tags`, optional `language`, `file_path`, `uri`, `description`

**MemoryEntryProto** fields: `id`, `content_type`, `content`, `source_agent`, `importance`, `tags`, `created_at` (int64), `updated_at` (int64), `key_scope`, `key_task_id`, `key_project_id`, `key`, optional `language`, `file_path`, `uri`, `description`

**HealthResponse** fields: `status`, `version`, `uptime_seconds` (uint64), `components` (repeated ComponentHealthProto)

### gRPC Server Details

- Binary: `uc-grpc-server` crate
- Default listen address: `[::]:50051` (overridable via `UC_GRPC_ADDR` env var)
- Uses `LocalEngine` with fallback to in-memory if storage backends unavailable
- Error mapping: `EngineError` variants map to tonic status codes (e.g., `SearchError` -> `Internal`, `IndexError` -> `NotFound`, `ConnectionError` -> `Unavailable`, `RateLimited` -> `ResourceExhausted`)
- All 9 RPCs are unary request/response -- no server streaming or client streaming

### gRPC Client Details (Rust)

- `GrpcEngineClient` in `crates/uc-grpc/src/client.rs`
- Implements `EngineApi` trait
- Connect via `connect(endpoint: &str)` or `from_channel(channel)`
- Full bidirectional type conversion between proto messages and uc-types

### Node.js gRPC Client Status

**No existing Node.js gRPC client code found in the repository.** The only gRPC-related JavaScript is in the built `tui/dist/cli.js` bundle, which includes the `@grpc/grpc-js` and `@grpc/proto-loader` packages as dependencies but does not yet use them.

The TUI `package.json` already declares the required dependencies:
- `@grpc/grpc-js`: `^1.12.0` (installed)
- `@grpc/proto-loader`: `^0.7.13` (installed)

### TUI Current Mock Data Flow

The TUI App component (`tui/src/components/App.tsx`) uses React `useState` for all state:

1. **Chat messages**: `messages: ChatMessage[]` -- appended via `addMessage` callback
2. **Subtasks**: `subtasks: SubtaskItem[]` -- updated via `setSubtasks`
3. **Task description**: `taskDescription: string`
4. **Status**: `workerId`, `backend`, `progress` (completed/total)

**Data flow for task submission** (lines 69-119):
- User submits description via `TaskInput` -> `handleSubmit` callback
- `addMessage(createUserMessage(description))` -- adds user message to chat
- `setTimeout` (500ms) simulates decomposition, populates `MOCK_SUBTASKS`
- `simulateProgress` function uses nested `setTimeout` calls (1000ms + idx*1500ms for start, 2500ms + idx*1500ms for complete) to simulate subtask state transitions

**Mock data** (lines 32-37): Hard-coded `MOCK_SUBTASKS` array with 4 subtasks in various states.

**Comments in code**: Line 13 states "gRPC integration will be added in PR2"; line 141 states "In PR2 this will be replaced by gRPC event streaming."

### TUI Build Setup

- **Bundler**: esbuild via `build.mjs`
- **Entry**: `src/index.tsx`
- **Output**: `dist/cli.js` (ESM, node platform)
- **JSX**: automatic runtime
- **Alias**: `react-devtools-core` -> empty stub (avoids runtime error)
- **Banner**: Injects `createRequire`, `__filename`, `__dirname` shims for CJS compat
- **Dev mode**: `tsx watch src/index.tsx`
- **TypeScript**: ES2022 target, bundler module resolution, strict mode

### Important: Proto File Location

The proto file is at `crates/uc-grpc/proto/engine.proto`, NOT at `proto/ultimate_coders.proto`. The `proto/` directory at the repo root does not exist. The `build.rs` references `proto/engine.proto` relative to the crate directory.

### Proto Compilation for Node.js

The Rust side compiles the proto at build time via `tonic-build` in `build.rs`. For the Node.js TUI, the proto file will need to be loaded at runtime using `@grpc/proto-loader`, which reads `.proto` files dynamically. The proto file path from the TUI perspective would need to be resolved relative to the TUI project or copied/bundled.

### No Streaming RPCs

The current proto defines only unary RPCs. The TUI comments reference "gRPC event streaming" for PR2, but the proto does not yet include any `stream` keywords. This means either:
- Server-side streaming RPCs need to be added to the proto for real-time event updates, or
- The TUI will need to poll unary RPCs (e.g., calling a status RPC periodically)

### Related Specs

- `.trellis/spec/frontend/state-management.md` -- Python-side state management patterns (task/subtask state machines)
- `.trellis/spec/frontend/directory-structure.md` -- Python package layout (not directly relevant to TUI)
- `.trellis/spec/frontend/type-safety.md` -- Python type conventions (not directly relevant to TUI)

## Caveats / Not Found

1. **No streaming RPCs exist yet** -- The proto only has unary calls. The TUI code comments reference "gRPC event streaming" for PR2, but the server does not expose any streaming endpoints. New streaming RPCs (e.g., `SubmitTask` returning a stream of `AgentEvent`) would need to be added to the proto.

2. **No task/subtask RPCs** -- The current proto covers search, index, memory, and health. There are no RPCs for task submission, subtask status tracking, or agent events. The TUI's primary use case (submit task, watch subtask progress) has no corresponding gRPC methods.

3. **Proto file path mismatch** -- The task description references `proto/ultimate_coders.proto` but the actual file is at `crates/uc-grpc/proto/engine.proto`.

4. **No Node.js gRPC client code exists** -- The `@grpc/grpc-js` and `@grpc/proto-loader` packages are installed as dependencies but no client code has been written yet.

5. **Build bundling concern** -- The esbuild bundler may have issues bundling `@grpc/grpc-js` because it uses native Node.js APIs and dynamic requires. The current `build.mjs` injects a `createRequire` shim, which may help, but this needs verification.
