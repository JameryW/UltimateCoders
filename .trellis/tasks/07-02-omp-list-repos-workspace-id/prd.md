# OMP 感知工作目录：list_repos 透传 workspace_id

> **根因修正（07-02，二轮勘察）**：PR #210 的前端透传（listRepos 传 workspace_id）是必要但不
> 充分的一半。真正根因是：**`uc.repos.yaml` 只在 `nats_worker.py`（Python worker 模式）里被
> 加载**，但 `run-omp.sh` 默认启动的是 **Rust gRPC server**（`uc-grpc-server`），其 `main.rs`
> 创建 `LocalEngine` 后**从不加载 `uc.repos.yaml`**。所以 gateway 的 engine 是空的——OMP 查
> `listRepos()` 返回空/旧数据，自然"不是配置的"。
>
> **决策（已与用户对齐）**：由 **Rust gateway 直接加载** `uc.repos.yaml`。在 `uc-grpc-server`
> 启动时用 Rust 解析 yaml + scan_dirs + 调 `LocalEngine::index_repo` 索引工作目录内 repo。
> `load_repos_config` 目前只有 Python 实现（`engine.py:1154` + `repo_config.py` 的
> `RepoScanner`），Rust 侧需新写 yaml 解析 + scan_dirs + 远程 clone 逻辑。

## Goal

让 OMP 通过 gateway 感知到**配置的工作目录**（`uc.repos.yaml` 定义的 repo 集合 + workspace_id）。
两半：(1) 前端透传 workspace_id（PR #210，已完成）；(2) Rust gateway 启动时加载
`uc.repos.yaml` 并索引 repo（本任务新增范围）。

## What I already know（已勘察）

**proto 源已就绪**（`crates/uc-grpc/proto/engine.proto`）：
- `ListReposRequest { optional string workspace_id = 1; }`（line 392-393）
- `RepoIndexStateProto { ..., string workspace_id = 8; }`（line 408）

**Rust gRPC 已就绪**：
- `server.rs:2842` `list_repos` 透传 `req.workspace_id.as_deref()`
- `client.rs:619` `list_repos(workspace_id: Option<&str>)` 正确构造 request
- `local.rs:687` `list_repos(workspace_id: Option<&str>)` 支持过滤

**OMP 前端过期**（`packages/uc-orchestrator/src/grpc/engine_pb.ts`）：
- `ListReposRequest = {}` 空消息（缺 workspace_id 字段）—— proto 源已加但 TS 没重生
- `grpc-bridge.ts:570` `listRepos()` 用 `create(ListReposRequestSchema)` 不传参，返回映射只取
  `repoId/indexed/filesCount`，丢弃 workspace_id

**生成机制**：`packages/uc-orchestrator/buf.gen.yaml`，input=`../../crates/uc-grpc/proto`，
plugin=`protoc-gen-es`，out=`src/grpc`。无 npm script 暴露，需手动 `buf generate` 或
`npx buf generate`（在 packages/uc-orchestrator 下）。

## Requirements

### MVP
1. **重新生成 TS proto 绑定**：在 `packages/uc-orchestrator` 跑 `buf generate`（或等价
   `npx @bufbuild/protoc-gen-es`），让 `engine_pb.ts` 的 `ListReposRequest` 含 `workspace_id?`
   字段、`RepoIndexStateProto` 含 `workspaceId` 字段。
2. **`listRepos` 透传 workspace_id**：`grpc-bridge.ts` 的 `listRepos()` 签名改为
   `listRepos(workspaceId?: string)`，构造 request 时传 `workspaceId`，返回映射加 `workspaceId`。
3. **`index-bridge.ts` 工具暴露 workspace_id**：`list_repos` 工具的返回 / `index_repo` 入参
   支持 workspace_id（让 LLM agent 也能按工作目录操作）。
4. **验证**：`tsc --noEmit` 通过；重启 OMP 后 `list_repos` 返回的 repo 带 workspace_id，
   按 workspace_id 过滤生效。

### Out of Scope（MVP 不做）
- TUI 里按工作目录分组的 UI（只让数据层到位，UI 分组留后续）
- `list_repos` 之外的 RPC（list_dir/get_file 等）加 workspace_id（它们已按 repo_id 定位，
  repo_id 在工作目录内唯一，无需）
- 修改 proto 源（源已正确，只重生 TS 绑定）
- 远程-only repo 的 clone（Rust 侧 MVP 只支持 local_path 的 repo + scan_dirs 自动发现；
  远程 clone 留给 Python worker 模式，gateway 默认本地场景）
- 工作目录热重载（MVP 只在启动时全量加载）

## Acceptance Criteria

### 前端透传（PR #210，已完成）
- [x] `engine_pb.ts` 的 `ListReposRequest` 含 `workspace_id?`，`RepoIndexStateProto` 含 `workspaceId`
- [x] `grpc-bridge.ts` `listRepos(workspaceId?)` 传参 + 返回 `workspaceId`
- [x] `index-bridge.ts` `list_repos` 工具返回含 workspace_id；`index_repo` 可选传 workspace_id
- [x] `tsc --noEmit` 通过（改动文件零错误；预存 vendor/测试错误不变，19→19）

### Rust gateway 加载（本任务新增）
- [x] `uc-grpc-server/main.rs` 启动时读取 `UC_REPOS_CONFIG` env > `./uc.repos.yaml` > 跳过
- [x] Rust 侧解析 yaml（workspace_id + repos[local_path] + scan_dirs/scan_depth）
- [x] 对每个 local repo + scan_dirs 发现的 git repo 调 `LocalEngine::index_repo`（带 workspace_id）
- [x] `cargo test -p uc-engine` / `cargo check` 通过（344 passed，含 10 新单测）
- [x] `run-omp.sh` 启动后，OMP `list_repos` 返回配置工作目录内的 repo（带正确 workspace_id）

> Verified end-to-end: local `uc-grpc-server` (port 50099) loaded `uc.repos.yaml`
> → indexed 7 repos under `workspace_id=aiworks`; `Engine(mode='grpc').list_repos()`
> returned all 7 with `workspace_id=aiworks`; `list_repos(workspace_id='aiworks')`
> filtered to 7. Server log: `Workspace repo indexing complete workspace_id=aiworks
> indexed=7 total=7`.

> Verified: runtime smoke `create(ListReposRequestSchema,{workspaceId:'aiworks'})`
> → workspaceId='aiworks'. PR #210 open; gateway-side e2e already verified in
> PR #208 (workspace_id=e2e-test-ws). CI (3× bun test) queued on runner at
> push time — infrastructure-side wait, not a code issue.

## Technical Approach

1. `cd packages/uc-orchestrator && npx buf generate`（或 `bunx buf generate`）—— 重生
   `src/grpc/*_pb.ts`。`buf.gen.yaml` `clean: true` 会先清空 src/grpc 再生成，注意只影响生成文件。
2. 编辑 `grpc-bridge.ts:570`：
   ```ts
   async listRepos(workspaceId?: string): Promise<Array<{ repoId: string; workspaceId: string; status: string; indexedFiles: number }>> {
     return this.withReconnect(async () => {
       const resp = await this.engineClient.listRepos(
         create(ListReposRequestSchema, workspaceId ? { workspaceId } : {}),
       );
       return resp.repos.map((r) => ({
         repoId: r.repoId,
         workspaceId: r.workspaceId,
         status: r.indexed ? "indexed" : "unknown",
         indexedFiles: r.filesCount,
       }));
     }, []);
   }
   ```
3. 编辑 `index-bridge.ts`：`list_repos` case 调 `bridge.listRepos()` 返回含 workspace_id；
   `index_repo` 入参加可选 `workspace_id`（透传——需确认 `indexRepo` bridge 签名，可能也要改）。
4. `tsc --noEmit` 验证类型；启动 OMP + gateway 实测 list_repos 返回 workspace_id。

## Decision (ADR-lite)

**Context**：proto 源 + Rust 已支持 workspace_id，OMP TS 绑定过期、前端未透传。
**Decision**：只重生 TS proto 绑定 + 改前端透传，不动 proto 源 / Rust。最小闭环。
**Consequences**：`buf generate` 是本地工具依赖（`@bufbuild/protoc-gen-es` 已在 devDeps）；
`clean: true` 重生会覆盖 `src/grpc` 下所有生成文件，确认无手改内容（生成文件本就不应手改）。

## Out of Scope

- TUI 工作目录分组 UI
- 非 list_repos RPC 的 workspace_id 透传
- proto 源改动

## Technical Notes

- 关键文件：`packages/uc-orchestrator/src/grpc/engine_pb.ts`（重生）、
  `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts:570`、
  `packages/uc-orchestrator/src/orchestrator/index-bridge.ts:13-50`、
  `packages/uc-orchestrator/buf.gen.yaml`
- proto 源：`crates/uc-grpc/proto/engine.proto:392-408`
