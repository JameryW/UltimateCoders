# PRD: RepoIndexState 补 remote_url/default_branch（审计 #8 Category-B 收尾）

## 背景

/loop 第 36 轮。承接 PR #336（ExecutionHistory proto 修复），本轮收尾最后一个 Category-B：RepoIndexState 的 remote_url/default_branch。修完后 dashboard tsc 盲点错误从 18（PR #335 起）-> 0。

## 清单（已核实 + 修复）

### F84: RepoInfo.remote_url/default_branch 恒 undefined（MED，真 bug）

`endpoints.ts:33-34` 读 `r.remoteUrl`/`r.defaultBranch`，但 `RepoIndexStateProto`（listRepos 返回元素）无此字段 -> TS2339，且运行时恒 undefined。

影响：
- `RepoManagementPanel:54-55` 把 `repo.remote_url`/`repo.default_branch` 传给 `indexRepo`（Reindex 按钮）-> Reindex 永远带 undefined 配置。
- 仓库列表不显示 remote/branch。

根因：数据其实**有**。`metadata_store.list_repos`（postgres.rs:417）返 `RepoSpec`，含 `remote_url`/`default_branch`/`local_path`（从 `repos` 表 SELECT）。但 `LocalEngine::list_repos`（local.rs:644）对每个 repo 调 `get_index_state(repo_id)`，而 `get_index_state` 只从 index pipeline 拿统计（files/symbols/chunks），**丢弃** RepoSpec 的 config（连 local_path 都硬编码 None）。`RepoIndexState` 结构体本身也没这俩字段。

## 修（全链路，数据在 RepoSpec 已就绪）

- **uc-types**（engine.rs `RepoIndexState`）：加 `remote_url: Option<String>`、`default_branch: Option<String>`。
- **uc-engine**（local.rs）：
  - `list_repos`：overlay RepoSpec 的 remote_url/default_branch 到 state（空串 -> None）。
  - `get_index_state` 两处构造（Some/None）：新字段填 None（由 list_repos overlay）。
- **uc-grpc**：
  - proto `RepoIndexStateProto`：加 `optional string remote_url = 9`、`default_branch = 10`（wire 兼容）。
  - conversions.rs 三处：`From<RepoIndexState> for RepoIndexStateProto` 映射新字段；`From<RepoIndexStateProto> for RepoIndexState` 映射回；`From<GetIndexStateResponse>` 填 None（该响应不带 config）。
- **regen** `dashboard/src/grpc/engine_pb.ts`（buf generate，仅 engine_pb.ts 变）。
- **dashboard**：`endpoints.ts` 已映射 `r.remoteUrl`/`r.defaultBranch`，proto 有字段后 TS2339 自解，无需改。

## 验收

- `cargo check`（全 workspace）通过；`cargo fmt --check` 干净；`cargo clippy` 无警告；`cargo test -p uc-types -p uc-engine -p uc-grpc` 全绿（353+121+6+17）。
- `tsc -p tsconfig.app.json --noEmit`：2 -> **0**（18 个盲点错全清）。
- `vite build` 通过。
- feature branch + PR + CI green（ci-rust / ci-dashboard / ci-python）。

## 不做

无。dashboard tsc 盲点错误已尽（18 -> 0）。后续可做 #11-#15 LOW 杂项或新审计。
