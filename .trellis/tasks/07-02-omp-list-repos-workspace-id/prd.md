# OMP 感知工作目录：list_repos 透传 workspace_id

## Goal

OMP 前端（uc-orchestrator）通过 `listRepos()` 看到的 repo 列表是**全表扁平列表**，不按
工作目录分组，也不回传 `workspace_id`。原因是 PR #208 给 proto 源（`engine.proto`）和
Rust gRPC（server/client）加了 `workspace_id` 支持，但**没重新生成 OMP 的 TS proto 绑定
`engine_pb.ts`**，且前端 `listRepos()` 没传 workspace_id、丢弃响应里的 workspace_id。
让 OMP 能按配置的 `workspace_id` 查询并展示 repo 的工作目录归属。

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

## Acceptance Criteria

- [ ] `engine_pb.ts` 的 `ListReposRequest` 含 `workspace_id?`，`RepoIndexStateProto` 含 `workspaceId`
- [ ] `grpc-bridge.ts` `listRepos(workspaceId?)` 传参 + 返回 `workspaceId`
- [ ] `index-bridge.ts` `list_repos` 工具返回含 workspace_id；`index_repo` 可选传 workspace_id
- [ ] `tsc --noEmit` 通过
- [ ] 重启 OMP 后 `list_repos` 返回的 repo 带正确 workspace_id（如 `aiworks`）

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
