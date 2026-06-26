# PRD: Wrap Distributed Coding Agent Capabilities as OMP Tools

## Goal

将底层分布式 coding agent 的核心能力封装为 OMP LLM-callable tools，使 OMP 中的 AI agent 能直接调用任务编排、内存管理、代码搜索、索引管理、调度等能力，无需人类手动 `/uc` 命令中转。

## Current State

已有 2 个 OMP tool:
- `uc_memory` — read/write/search（缺 delete, batch_write, list_keys, importance/tags/content_type 扩展参数）
- `uc_search` — 基础 hybrid search（缺 repo/language/path 过滤、索引管理）

已有 `/uc` slash command: submit/status/cancel/pause/resume（非 LLM-callable）

底层 Rust gRPC bridge 已有但未暴露: DeleteMemory, BatchWriteMemory, ListRepos, IndexRepo, GetIndexState, RemoveIndex, ListDir, GetFile, CancelTask, UpdateTask, WatchTask

## Design Principles

1. **Tool = LLM-callable** — OMP agent 自主调用，不需要用户 `/uc` 中转
2. **Bridge first** — 优先补齐 GrpcBridge 缺失的方法，再在 bridge 上注册 tool
3. **分组注册** — 按能力域拆分 register 文件，避免 memory-bridge.ts 膨胀
4. **Ponytail** — 最小参数集，不过度封装；每个 tool 的 schema 足够描述语义即可

## Tool Inventory

### Phase 1: 补齐现有 tool + 高频缺失（核心价值）

| Tool | 描述 | Bridge 补充 |
|------|------|-------------|
| `uc_task` | 任务生命周期: submit/cancel/pause/resume/status | 补 CancelTask |
| `uc_memory` 扩展 | 增加 delete action, importance/tags/content_type 参数 | 补 DeleteMemory |
| `uc_search` 扩展 | 增加 repo_ids/languages/path_patterns 参数 | — |
| `uc_index` | 索引管理: index_repo/list_repos/get_state/remove_index | 补 IndexRepo, GetIndexState, RemoveIndex, ListRepos |

### Phase 2: 编排 + 协调能力

| Tool | 描述 | Bridge 补充 |
|------|------|-------------|
| `uc_file` | 文件读取: list_dir/get_file | 补 ListDir, GetFile |
| `uc_schedule` | 调度: cron/one-shot/night-window | 新增 SchedulerService RPC (暂走 Python RPC) |

### Phase 3: 低频 / 监控类（可后置）

| Tool | 描述 | 备注 |
|------|------|------|
| `uc_checkpoint` | 显式 checkpoint/recover | 自动 wave boundary 已覆盖，显式操作低频 |
| `uc_circuit_breaker` | 查询断路器状态 | 监控用途，agent 很少主动调用 |
| `uc_rate_limiter` | 查询限流状态 | 同上 |

Phase 3 本 PR 不做，记录在案即可。

## Implementation Plan

### 1. GrpcBridge 补齐方法

在 `grpc-bridge.ts` 新增:
- `cancelTask(taskId: string): Promise<boolean>`
- `deleteMemory(keyScope, key, taskId?, projectId?): Promise<boolean>`
- `indexRepo(repoId, localPath, languages?): Promise<boolean>`
- `getIndexState(repoId): Promise<IndexState | null>`
- `removeIndex(repoId): Promise<boolean>`
- `listRepos(): Promise<string[]>`
- `listDir(path, repoId?): Promise<DirEntry[]>`
- `getFile(path, repoId?): Promise<string | null>`

resolveService 已注册这些 method → TaskService/EngineService，不需要改。

### 2. 新建 tool 注册文件

- `packages/uc-orchestrator/src/orchestrator/task-bridge.ts` — `registerTaskTools(pi, bridge, orchestrator)`
- `packages/uc-orchestrator/src/orchestrator/index-bridge.ts` — `registerIndexTools(pi, bridge)`
- `packages/uc-orchestrator/src/orchestrator/file-bridge.ts` — `registerFileTools(pi, bridge)`
- `packages/uc-orchestrator/src/orchestrator/schedule-bridge.ts` — `registerScheduleTools(pi, bridge)` (Phase 2)

### 3. 扩展现有 tool

`memory-bridge.ts`:
- `uc_memory` schema 增加: `action` 枚举加 `"delete"`; 可选参数 `importance`, `tags`, `content_type`
- Bridge `writeMemory` 增加 importance/tags 参数传递

`uc_search` schema 增加: `repo_ids`, `languages`, `path_patterns` 可选数组参数

### 4. extension.ts 改动

```ts
import { registerTaskTools } from "./orchestrator/task-bridge";
import { registerIndexTools } from "./orchestrator/index-bridge";
import { registerFileTools } from "./orchestrator/file-bridge";

// 在 registerMemoryTools 之后:
registerTaskTools(pi, bridge, orchestrator);
registerIndexTools(pi, bridge);
registerFileTools(pi, bridge);
```

### 5. Tool Schema 设计

**uc_task:**
```ts
{
  action: "submit" | "cancel" | "pause" | "resume" | "status",
  task_id: string,          // required for cancel/pause/resume/status
  description: string,      // required for submit
}
```

**uc_index:**
```ts
{
  action: "index_repo" | "list_repos" | "get_state" | "remove_index",
  repo_id: string,          // for index_repo/get_state/remove_index
  local_path: string,       // for index_repo
  languages: string[],      // optional for index_repo
}
```

**uc_file:**
```ts
{
  action: "list_dir" | "get_file",
  path: string,
  repo_id: string,          // optional
}
```

**uc_schedule (Phase 2):**
```ts
{
  action: "create_cron" | "create_one_shot" | "cancel" | "list" | "set_night_window" | "clear_night_window",
  description: string,      // for create_*
  cron_expression: string,  // for create_cron
  execute_after: string,    // ISO timestamp for create_one_shot
  task_id: string,          // for cancel
  night_start: string,      // HH:MM for set_night_window
  night_end: string,        // HH:MM for set_night_window
  timezone: string,         // optional
}
```

## Out of Scope

- Phase 3 tools (checkpoint, circuit_breaker, rate_limiter)
- Worker registration/query tool（agent 不会动态注册 worker）
- Edit intent/conflict tool（属于 worker 间协调，不由 LLM tool 驱动）
- Sandbox 直接执行 tool（安全风险，暂不暴露）
- WatchTask streaming（OMP tool 是请求-响应模式，不适合 streaming）

## Testing

每个 bridge 方法补齐后，在 `tests/` 下加对应集成测试（需要 gRPC server running）。
Tool 注册用 TypeScript 编译检查 + 手动 `./run-omp.sh` 验证 tool 列表。
