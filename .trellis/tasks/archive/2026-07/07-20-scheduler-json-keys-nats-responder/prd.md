# PRD: 修正 scheduler json_to_* key 映射（对齐 NATS responder）

## 背景

/loop 第 39 轮。本轮继续 json_to_* 审计时发现：PR #336 / #339 的 key 映射改错了发送方。需回退到匹配真实 responder。

## 根因（已核实）

Rust `DashboardService.get_scheduler_status` 走 `nats_dashboard_request("GetSchedulerStatus")`（dashboard_service.rs:105），NATS subject `uc.dashboard.GetSchedulerStatus`。responder 是 `nats_worker._dash_getschedulerstatus`（nats_worker.py:2236，订阅 `uc.dashboard.>` line 469），它序列化：

- job: `{id, name, cron, enabled, last_run, next_run}`（line 2248-2252）
- execution_history: `{job_id, job_name, executed_at, success, error}`（line 2256-2259）
- night_window: `{start, end, enabled}`（line 2245）

`app.py._get_scheduler_data`（REST/snapshot 路径）用**不同**键（`description/cron_expression/execute_after`、`task_id/started_at/status`、`{active}`），但 app.py **不订阅 `uc.dashboard.>`**（只订阅 `uc.task.event`）-> app.py 不是此 RPC 的 responder。

PR #336/#339 误把 app.py 当 responder，把 Rust key 改成 app.py 的键（`description/cron_expression/execute_after`、`task_id/started_at/status`），与真实 responder（nats_worker）的键不匹配 -> 若 scheduler 上线会读到空值。

附注：`orch.scheduler` 当前是 `None` stub（orchestrator.py:111-112 "scheduler stub"），且 nats_worker 的 handler 用属性访问（`sched.jobs`/`sched.execution_history`）而 Python Scheduler 只有方法（`list_jobs()`/`get_execution_history()`）-> 路径当前 inactive（scheduler 面板恒 unavailable）。故无可见回归，但 key 映射须改正以对齐 responder。

## 修（回退 key 映射到 nats_worker）

- `json_to_scheduled_job`：`name<-"name"`、`cron<-"cron"`、`next_run<-"next_run"`（原值，匹配 nats_worker）。
- `json_to_execution_history`：`job_id<-"job_id"`、`job_name<-"job_name"`、`executed_at<-"executed_at"`、`success<-json_bool("success")`（原值，匹配 nats_worker）。
- 保留 PR #336 新增的 optional 字段读取（`started_at`/`completed_at`/`result_summary`/`status`）--nats_worker 暂不发，为 None；若将来 responder 增强发送则自洽。也保留 PR #336 的 proto 字段、app.py None 修复、dashboard 映射、executionStatusColor（均无害）。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-grpc）干净；`cargo test -p uc-grpc` 121+6 全绿。
- `tsc -p tsconfig.app.json --noEmit` 0；`vite build` 通过。
- feature branch + PR + CI green（ci-rust）。

## 不做

scheduler 路径激活（wire `orch.scheduler` + 修 nats_worker handler 属性访问 + 可能增强 responder 发送 started_at/completed_at）是独立大功能，不在本轮。
