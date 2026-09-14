# T7: node/attempt 级取消 + cancel-attempt-keep-node 原语

- Issue: #643
- Blocked by: T6（已落地，#642 关闭）
- Design refs: §3 G6, D6 (#635) 第 2/3 点
- 日期: 2026-09-14

## Goal

取消下沉到 node/attempt 粒度：proto 与 gateway 支持粒度参数，worker 具备
进程组级协作取消能力，cancel-attempt-keep-node 成为正式原语（pause 宽限
超时是其首个内部调用方），TS cascade 簿记删除、级联语义上移 gateway。

## 勘察基线

见 `research/recon-2026-09-14.md`（Explore 报告，含 6 个与票面假设的偏差）。
关键事实：`CancelTaskRequest` 现仅 task_id；`NatsTaskEvent` 有 subtask_id 槽位
无 attempt 字段；Python worker 无进程组 kill 能力（spawn 无 start_new_session，
os.killpg 零命中）；`fail_running_attempts` 是 graph 整批原语、无单点签名；
TS subtask 级 cancel 纯本地（与 task 级 RPC-first 不对称）。

## 关键设计裁决（勘察后定稿）

1. **两种取消语义分离**（状态机依据：CANCELLED 是 terminal 无出边，
   graph.rs:81-86；RUNNING→READY 是 fence re-arm 边）：
   - **attempt 级 cancel**（`attempt_no` 指定）= cancel-attempt-keep-node：
     attempt fence（epoch 语义同 fail_attempt），node 回 READY 重派。
     验收一（"RUNNING attempt 取消 → node 回 READY → 重派 commit"）即此。
   - **node 级 cancel**（仅 `subtask_id`）= node → `CANCELLED`（terminal）
     + downstream 依赖闭包 → CANCELLED（闭包在 gateway 用 graph_nodes
     依赖表计算，兄弟节点不碰）。用户取消的 node 不得回 READY 重派，
     否则取消无意义。
   - task 级（两者皆缺省）= 现状语义（全部 Failed）。
2. **cancel attempt 复用 fail_attempt 的迁移语义**（attempt 行终态 FAILED +
   reason，budget 按 RearmedToReady/NodeFailed 现状走）——与 T6 pause-grace
   已交付行为连续，不引入第三种预算语义。attempt 的 reason 字符串区分
   `cancelled` / `pause_grace_expired`。
3. **控制面命名**：NatsTaskEvent 沿用 `subtask_id` 槽位承载 node_id
   （graph_id == task_id 身份映射 T6 已确立），新增 `data` 约定键
   `attempt_no`（= dispatch_retry_count 序号）与 `reason`。事件类型新增
   `subtask_cancelled`（node 级）与 `attempt_cancelled`（attempt 级）；
   `task_cancelled` 保持不变。
4. **worker 进程组**：POSIX `start_new_session=True` + `os.killpg`；
   (task_id) → running attempt 登记（node_id/attempt_no → asyncio.Task +
   proc 句柄）；Windows 开发环境 fallback 现有单进程 kill（生产 worker 为
   POSIX）。杀完后的结果上报走既有 partial fenced 路径——被 fence 拒绝是
   预期行为（验收二）。
5. **TS subtask cancel 改 RPC-first**（对齐 task 级）：走扩展后的
   bridge.cancelTask(subtaskId)；`reverseCascadeUnCancel` 随 cascade 家族
   删除，`retrySubtask` 改为只重置目标子任务（重派权威在服务端）。

## Scope（C1–C5 切片，按此顺序提交，`Tracker: #643`）

### C1 — proto 扩展 + 单点原语
- engine.proto：`CancelTaskRequest` 增 `optional string subtask_id = 2;`、
  `optional uint32 attempt_no = 3;`（proto3 optional → 判别式存在性）。
- graph_store 新增单点原语 `cancel_attempt(graph_id, node_id, attempt_no,
  reason) -> FailOutcome`（复用 fail_attempt_tx 迁移路径，区别仅在 reason
  与调用方语义）；node 级闭包计算辅助（downstream 遍历 graph_nodes 依赖）。
- 三条 stub 管线再生成：cargo build（Rust）/ `buf generate`（orchestrator
  包 + 根，TS/Dashboard）。
- graph_store 单测：cancel_attempt 三分支（Fenced / RearmedToReady /
  NodeFailed）+ 迟到结果被拒。

### C2 — gateway 粒度处理
- `TaskStore::cancel_task` / gRPC handler 按 subtask_id/attempt_no 分派：
  attempt 级 → cancel_attempt + `revert_swept_subtask` 桥接；node 级 →
  闭包计算 + 逐点取消（graph CANCELLED + legacy 镜像同步）。
- NatsTaskEvent 扩展（data 键约定）+ `publish_task_status_event` 支持新
  类型与粒度载荷。
- pause-grace 切换：`spawn_pause_grace_timer` 在 graph-plane 硬停后补发
  `attempt_cancelled` 控制事件（worker 杀进程的触发点）——权威侧 fence
  行为不变（验收基线是 T6 diamond 测试继续绿）。
- server.rs 单测：粒度分派 / 闭包不误伤兄弟 / 事件载荷。

### C3 — worker 协作取消
- spawn 链加 `start_new_session=True`（POSIX）；登记 (task_id → {node_id:
  (asyncio.Task, proc)})。
- `_handle_task_event` 升级：`subtask_cancelled`/`attempt_cancelled` →
  匹配 attempt 的 killpg + asyncio cancel + fenced 上报；task_cancelled
  保持现有 task 级行为。
- pytest：事件分派 / 进程组杀 / malformed guard 扩展。

### C4 — TS cascade 退役
- 删 `cascadeCancel`/`reverseCascadeUnCancel`（实例 + 模块级纯函数）+
  reverse-cascade-un-cancel.test.ts 整文件。
- subtask 级 cancelTask 改 RPC-first（bridge.cancelTask 带粒度参数，
  stub 更新后接线）；retrySubtask 只重置目标。
- extension/uc-rpc-server surface 透传新参数；control-outcome /
  claim-loop 测试适配。

### C5 — 端到端验收
- Rust 集成（#[ignore] 模式，pause_grace_diamond.rs 同级或扩展）：
  ① RUNNING attempt 取消 → node READY → 重派 commit；② 迟到结果被
  fencing 拒；③ node 级取消不误伤兄弟 + 闭包正确。
- 全量门禁：fmt/clippy 五目标、Rust 基线、pytest 977+4、TS 162、tsc。

## Test seams

- C1 的 graph_store 单测（无 PG：in-memory 或现有测试夹具模式）。
- C5 的 #[ignore] PG 集成（与 T4/T6 欠账同批实跑）。
- Python worker 事件分派走既有 fake-NATS 模式（test_nats_jetstream_subtask.py）。
- TS：control-outcome / claim-loop 适配 + 新 verb 的 upsert/取消交互。

## 门禁

- fmt / clippy `-D warnings` 五目标；Rust 基线 437+5 / 379+5 / 192+8 / 36 / 35（只增不减）。
- pytest 977+4；TS 162 pass / 17 files；tsc 本包零错误。
- PG 集成（含 T4/T6 欠账 + 本票新增）在 Docker 恢复后实跑。

## Out of scope

- MergeArbiter 迁移与 D5 commit barrier（P1）。
- Dashboard UI 新交互（Dashboard stubs 再生成属 C1，新按钮/流程不在本票）。
- Windows 进程组 kill 的完整支持（fallback 单进程 kill 即可，生产为 POSIX）。
- TS retrySubtask 的服务端 retry RPC 化（本票只删 reverseCascade 依赖，
  retry 语义重立归后续）。
