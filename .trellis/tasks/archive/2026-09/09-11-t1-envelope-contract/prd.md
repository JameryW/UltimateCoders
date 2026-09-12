# T1: 执行信封契约 + contract_version 握手

Tracker: #637（验收以其为准；依赖边：#638 T2 blocked by 本票）
Design refs: docs/architecture/durable-runtime-migration-assessment.md §4 D3/D4；#633 resolution

## Goal

给 subtask 派发装上未来图运行时的**信封**（行为不变化），并把 **contract_version 握手**做成硬门禁——gateway/worker 版本混跑时必须响亮地失败，不再静默拒单（D3 一刀切的执法件）。

## Scope

1. **信封字段（additive）**：`NatsSubtaskExecute` 新增 `graph_id / node_id / attempt_id / idempotency_key / worker_epoch / contract_version`，全部 `#[serde(default)]`。过渡期恒等映射：`graph_id=task_id`、`node_id=subtask_id`、`attempt_id=retry_no`（现阶段取 `dispatch_retry_count`）、`worker_epoch=""`（T3 前无 epoch 概念）。**两个发布器都覆盖**：server.rs `publish_ready_subtasks` 与 dispatcher.rs L199 legacy `json!`。
2. **idempotency_key 规范**：`sha256("{graph}:{node}:{attempt}")[:32]`，确定性、无时间戳成分。本票只定义+产出，**不**改 message_id、不设 `Nats-Msg-Id`（T4）。
3. **握手（本票唯一行为变更）**：
   - proto `RegisterWorkerRequest` + `WorkerHeartbeatRequest` 各加 `string contract_version`（下一空闲字段号）；Rust 常量 `CONTRACT_VERSION="v1"` 放 uc-types。
   - gateway `RegisteredWorker` 存 contract_version；注册版本不匹配 → `RegisterWorkerResponse{success=false, error=...}`。
   - 派发门控：`publish_ready_subtasks` 只为 contract_version 匹配的 worker 置 Assigned/发布；全部不匹配 → 节点保持 Pending + `tracing::warn` + 事件（可观测，不静默）。NATS heartbeat 路径的 `w_info` 同样带版本并同样门控 stale 逻辑。
   - Python：`_registration_metadata` 加 `contract_version` 键（文档承诺 stable keys，additive 合法）；gRPC/PyO3/client 链透传；`uc.heartbeat` w_info 加同键。
   - worker 对**无信封**消息本票保持宽容（记录 log），拒收语义归 T4（勘察证实双向 additive）。
4. **文档**：AGENTS.md / 部署文档跨主机章节加 lockstep 升级顺序说明（D3 约束 3）。

## Out of scope

`Nats-Msg-Id`、worker 去重、fencing、字段删除（T4）；图表（T2）；Executor trait（T5）。

## Test seams

- Rust 单测（uc-grpc，messaging feature）：信封确定性（同 (graph,node,attempt) 两次构建 byte-identical）；两个发布器都产信封；握手不匹配 → 注册被拒 + 不派发；版本匹配 → 正常。
- Python 单测（tests/python/test_nats_worker_helpers.py 风格）：`_registration_metadata`/`w_info` 含 contract_version；worker 解析带信封消息不回归。
- 质量门：`cargo fmt --all`；`cargo test -p uc-engine`（基线 423）+ `-p uc-grpc --all-features`（基线 172+8 量级）+ no-default-features；clippy -D warnings；dashboard `pnpm run build`（从**仓库根**跑 buf generate 若需）；orchestrator `pnpm run check` 若 TS 有改动。

## Agreed decisions

- 加字段即安全（research §3 双向 additive 证实）→ 不引入 feature flag。
- 握手放 proto 显式字段而非 metadata JSON 解析（metadata 是自由字符串，执法要可靠）。
- TS 桩预期零 diff（additive）；若 codegen 实际产生 diff 则一并提交（改 proto 必查两份桩的旧规仍执行：orchestrator 从其目录、dashboard 从仓库根）。

## Accepted deviations（check 阶段记录，2026-09-11）

1. **warn-only**：本票"tracing::warn + 事件"中的事件通知未实现（需新增跨语言 AgentEventType，超出"行为不变"范围）。由 check 判定可接受：注册期 warn + 派发期逐次 warn 已满足"非静默"底线；用户可见的混版本告警并入 T4 的混版本可见性票面。
2. **第三个发布器**：Python `_dispatch_remote()` 原不在票面范围，但它证伪了"每条 uc.subtask.execute 都带信封"的契约声明，故本票直接修复（`_execution_envelope()` + 跨语言 golden 测试），不是遗留 follow-up。
3. TS 桩实际产生 diff（+20/−1 ×2），已按上述约定随 proto 提交。
