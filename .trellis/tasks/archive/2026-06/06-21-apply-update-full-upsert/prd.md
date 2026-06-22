# 双状态源同步：apply_update 完整 upsert

## Goal

Rust TaskStore 的 `apply_update` 当前只更新 status 和 subtask status，不同步 description、result、depends_on 等字段。Python 侧的状态变化无法完整反映到 Rust 侧，导致 TUI/Dashboard 看到的数据不完整。

## Requirements

1. `NatsTaskUpdate` protobuf/Rust struct 扩展：添加 subtask 的 `description`、`depends_on`、`result` 字段
2. Python `_make_task_update_payload` 填充这些新字段
3. Rust `apply_update` 做完整 upsert：更新 task status、result；对 subtask 做 upsert（存在则更新全部字段，不存在则创建）
4. 向后兼容：旧格式的 NatsTaskUpdate（缺少新字段）仍然正常处理

## Acceptance Criteria

- [ ] Python 侧 subtask 的 description/depends_on/result 通过 NATS 同步到 Rust TaskStore
- [ ] TUI/Dashboard 通过 ListTasks/WatchTask 能看到完整的 subtask description
- [ ] 旧格式 NATS 消息（无新字段）不报错，用默认值
- [ ] cargo test + pytest 通过

## Out of Scope

* Rust → Python 反向同步（pause/resume 通知已在 uc.task.event 通道）
* 本地降级路径（LocalWorkerBridge）的同步
* PostgreSQL TaskStoreBackend 的 upsert 实现

## Technical Notes

* Rust: `crates/uc-grpc/src/server.rs` — `NatsTaskUpdate`, `NatsSubtaskUpdate`, `apply_update`
* Python: `python/ultimate_coders/nats_worker.py` — `_make_task_update_payload`, `_subtask_status_to_nats`
* Proto: `proto/engine.proto` 可能需要扩展（或直接在 JSON 层面添加字段，不走 proto）
* 当前 `apply_update` 在 `crates/uc-grpc/src/server.rs:445-515`
