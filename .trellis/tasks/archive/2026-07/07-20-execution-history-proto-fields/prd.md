# PRD: 执行历史 proto 字段补全 + 字段名不匹配修复（审计 #8 Category-B 一半）

## 背景

/loop 第 35 轮。承接 07-20-dashboard-tsc-blindspot-fixes（PR #335），本轮处理其中推迟的 Category-B 之一：ExecutionHistory。RepoIndexState 的 remote_url/default_branch 仍推迟（后端 RepoIndexState 不带 config，需设计决策）。

## 清单（已核实 + 修复）

### F83: ExecutionHistory 全链路数据丢失（HIGH，真 bug）

`SchedulerPanel` 执行历史本来全坏：每条 `task_id=""`、`status="failed"`、无时间/时长。根因是三处错叠：

1. **字段名不匹配**（Rust `json_to_execution_history`）：读 `job_id/job_name/executed_at/success/error`，但 Python（`_get_scheduler_data`）序列化的键是 `task_id/started_at/completed_at/status/result_summary`。`json_str` 缺省返 `""`，`json_bool` 缺省返 `false` -> `job_id=""`、`success=false`（恒 "failed"）。
2. **proto 字段缺失**：`ExecutionHistoryProto` 只有 `job_id/job_name/executed_at/success/error`，无 `started_at/completed_at/result_summary` -> dashboard `e.startedAt` 等报 TS2339，时间/时长恒 undefined。后端 Rust `ExecutionHistory`（uc-types/scheduler.rs:142）其实**有** `started_at/completed_at/result_summary`，PyExecutionHistory 也暴露了，只是 proto 没带。
3. **Python None 序列化 bug**：`str(getattr(hist, "completed_at", ""))`，`completed_at` 是 `Option<String>`，None 时 `str(None)="None"` -> JSON 字符串 `"None"`。
4. **status 大小写**：后端返 PascalCase（`"Completed"/"Failed"/"Skipped"/"Deferred"`），dashboard `executionStatusColor` 只认 lowercase `completed/failed/in_progress`，且旧映射产 `"success"`（无匹配 -> 灰）。

## 修（全链路）

- **proto**（engine.proto `ExecutionHistoryProto`）：加 `optional string started_at = 6`、`completed_at = 7`、`result_summary = 8`、`status = 9`（新 optional 字段，wire 兼容）。
- **Rust**（`json_to_execution_history`）：按 Python 真实键映射--`job_id←task_id`、`executed_at←started_at`、`success = (status=="Completed")`，并填 `started_at/completed_at/result_summary/status`。
- **Python**（app.py）：去掉 `str()` 包裹，`getattr(..., None)` 让 None -> JSON null（修 `completed_at` 的 `"None"`）。
- **regen** `dashboard/src/grpc/engine_pb.ts`（`buf generate`，仅 engine_pb.ts 变）。
- **dashboard**：`grpcExecutionHistoryToDashboard` 用 `e.status ?? (e.success ? "success" : "failed")`（优先真实 status）；`executionStatusColor` 改 `status.toLowerCase()` 并补 `skipped/deferred/success` 分支。

## 验收

- `cargo check -p uc-grpc -p uc-grpc-server` 通过；`cargo test -p uc-grpc` 121+6 全绿。
- `tsc -p tsconfig.app.json --noEmit`：5 -> 2（所触文件零新错；剩 2 是 RepoIndexState remoteUrl/defaultBranch，下轮）。
- `vite build` 通过；`ruff check` 通过。
- feature branch + PR + CI green（ci-rust / ci-dashboard / ci-python）。

## 不做（下轮）

`RepoIndexStateProto` 无 `remoteUrl`/`defaultBranch`（仅 `IndexRepoRequest` 有，后端 `RepoIndexState` 也不带 config）-> `RepoInfo.remote_url`/`default_branch` 恒 undefined。需后端把 repo config 纳入 index state（或单独 RPC）+ proto 扩展，跨设计决策，推迟。
