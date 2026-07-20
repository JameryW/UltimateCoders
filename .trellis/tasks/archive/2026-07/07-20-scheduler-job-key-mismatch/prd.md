# PRD: Scheduler job JSON key 不匹配（json_to_scheduled_job）

## 背景

/loop 第 38 轮。承接 PR #336（修了 json_to_execution_history 的 key 不匹配）。本轮系统排查同模式 bug，发现 json_to_scheduled_job 同病。

## 清单（已核实 + 修复）

### F87: json_to_scheduled_job key 不匹配 -> 作业 description/cron 恒空（MED，真 bug）

`json_to_scheduled_job`（dashboard_service.rs:470）读 `name/cron/last_run/next_run`，但 Python `_get_scheduler_data`（app.py:1323-1335）序列化作业为 `{id, description, project_id, enabled, cron_expression?, execute_after?}`。`json_str` 缺省返 `""`、`json_opt_str` 返 None：

- `name <- "name"`：Python 无 "name" -> 恒 ""。
- `cron <- "cron"`：Python 发 "cron_expression" -> 恒 ""。
- `next_run <- "next_run"`：Python 发 "execute_after" -> 恒 None。

proto ScheduledJobProto 拿到 name=""、cron=""、next_run=None。dashboard `grpcScheduledJobToDashboard` 映射 `description<-name`、`cron_expression<-cron||undefined`、`execute_after<-nextRun??undefined` -> SchedulerPanel 显示作业 description 空、cron 不显示。`id`/`enabled` 本就对。

## 修（纯 Rust，proto/dashboard 已对齐，无需改）

`json_to_scheduled_job` 按真实键映射：
- `name <- json_str(v, "description")`
- `cron <- json_str(v, "cron_expression")`
- `next_run <- json_opt_str(v, "execute_after")`
- `id`/`enabled` 不变；`last_run <- "last_run"`（Python 不发，None，保持）。

dashboard `grpcScheduledJobToDashboard` 已 `description<-j.name`、`cron_expression<-j.cron||undefined`、`execute_after<-j.nextRun??undefined` -> 填对后自洽。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-grpc）干净；`cargo test -p uc-grpc` 121+6 全绿。
- `tsc -p tsconfig.app.json --noEmit` 0；`vite build` 通过。
- feature branch + PR + CI green（ci-rust / ci-dashboard / ci-python）。

## 不做（下轮）

`json_to_night_window` 同模式不匹配：Rust 读 `start/end/enabled`，Python 只发 `{active}`。但 night_window_start/end 是**按作业**的 getter（PyScheduledTask），scheduler 级只有 active 标志，无单一 start/end。修它需语义决策（scheduler 级 night window 配置从哪取）+ 可能加 PySchedulerService getter，推迟。
