# T12 勘察笔记

## 派发链现状

- **两个发布嘴**（结构同构，各持一份重复的 gate+发布逻辑）：
  - `GrpcServer::publish_ready_subtasks`（server.rs:2663）——分解后 / `uc.task.update`
    后触发。
  - `dispatch_ready_subtasks`（自由函数，server.rs:3709）——NATS 订阅处理
    `uc.task.update` 后、以及 heartbeat monitor 的图平面 sweep 桥后触发。
  - 两者都是：`get_ready_subtasks` → 逐个 `registry.dispatch_gate(req_caps, project_id)`
    → `update_subtask_status(Assigned)` → `subtask_execute_payload(...)` →
    `publish_with_headers(NATS_SUBJECT_SUBTASK_EXECUTE, dedup, bytes)`。
  - **发布 subject 目前硬编码常量** `NATS_SUBJECT_SUBTASK_EXECUTE = "uc.subtask.execute"`
    （server.rs:51）；`NatsExecutor`（uc-engine/scheduler/executor.rs:112）同样硬编码
    `"uc.subtask.execute"`（且带 `with_subject` 覆写口，测试用）。
    注意 `NatsExecutor` 是 T5 的 Executor trait 侧派发路径，本票**不改**它（票面只
    要求网关打分 + overflow；ExecutorSelector 路径的定向留作后续），在 notes 记明。
  - 另一处 legacy 发布：`uc-engine/src/scheduler/dispatcher.rs:244`
    `let subject = "uc.subtask.execute".to_string();`（scheduler 分层派发）——同样不在本票
    范围内（非 gateway dispatch mouth）。

- **硬闸**：`WorkerRegistry::dispatch_gate(required, project_id) -> WorkerDispatchGate`
  （worker_service.rs:225）。内部 = `workers_with_capabilities`（capability + 可用性）→
  `serves_scope` → `contract_version == CONTRACT_VERSION`，四种变体
  （Dispatch / NoCapableWorker / NoScopeMatchedWorker / NoVersionMatchedWorker）。
  ⇒ 打分必须复用**同一套过滤**，否则会把节点定向到 gate 不会放行的 worker。

- **候选可用性**：`RegisteredWorker::is_available(STALE_TIMEOUT_SECS=60)`
  = 心跳新鲜 **且** `current_load < max_capacity`。所以过期 worker 天然不在候选里，
  "所有 worker 过期 → 共享 subject" 自动成立。

## worker 侧 transport（per-worker consumer 的落点）

- `nats_worker.py::_ensure_subtask_transport`（line 925）：`js.add_consumer(stream=
  UC_SUBTASKS, durable_name=共享 durable, ack_policy="explicit", max_deliver=5)` +
  `js.pull_subscribe(NATS_SUBJECT_SUBTASK_EXECUTE, durable=..., stream=...)`，
  成功后 `_subtask_js_available=True` → **才**允许 `_register_with_gateway()`
  （T5/D4 Q1：transport 未绑定不得注册）。per-worker consumer 必须挂在这条
  "先绑定、后注册"的路径上，注册时才能诚实声明 `per_worker_topic=true`。
- 绑定失败只重试，不致命（循环 sleep 5s）。

## 心跳现状

- gRPC 心跳：`nats_worker.py::_heartbeat_loop` line 2085 →
  `engine.worker_heartbeat_async(worker_id, load, CONTRACT_VERSION)` →
  pyo3 `Engine::worker_heartbeat_async`（engine.rs:1279，`#[pyo3(signature =
  (worker_id, current_load, contract_version=None))]`）→
  `GrpcEngineClient::worker_heartbeat`（client.rs:345）→ proto
  `WorkerHeartbeatRequest{worker_id=1, current_load=2, contract_version=3}`。
- NATS 侧心跳（`uc.heartbeat`，line 2069）另有一套 JSON w_info——本票不碰
  （registry 只认 gRPC 心跳；D8/T8 的 projects 也走 gRPC 注册）。
- **host 信号已存在**：`_registration_metadata()`（nats_worker.py:2196）输出
  `{"hostname": socket.gethostname(), "pid", "contract_version", "subtask_transport",
  [compose_project]}`，注释明确"Keys are stable API"。⇒ locality 直接读
  `metadata["hostname"]`，**无需新增 wire 字段**。

## recent_files 的数据来源（Python）

- worker 今天没有"最近文件"概念。`Subtask.file_constraints` 在执行前后都在手边；
  `SubtaskResult.file_changes`（`FileChange.file_path`）是执行产出的真实改动文件。
  ⇒ 在 `Worker._execute_in_sandbox` 完成处记录
  `file_constraints ∪ [fc.file_path for fc in output.file_changes]`，有界（上限 64、
  最新在前、去重）。`_execute_steps` 多步链的合并结果在 `all_file_changes`。
- 选择"执行期记录"而不是"结果上报后记录"：结果上报路径（`_execute_and_report`）
  已有 T10/T11 的既有多处 mock 断言，动它成本高；`_execute_in_sandbox` 是执行收口。

## 为什么不把 per-worker consumer 交给网关创建

票面原文是"per-worker durable consumers, provisioned at worker registration"。
实现时改为 **worker 自建 + 注册/心跳声明**，理由：

1. JetStream durable consumer 的 `filter_subject` **不可变**：网关与 nats-py
   必须逐字段一致才不会撞 "consumer already exists with different configuration"；
   两语言各写一份配置、本票又没有 live NATS 集成测试（PG/NATS 实跑目前是欠账），
   一旦不一致就是**运行时才暴露**的派发静默失败。
2. worker 侧的"先绑定 transport，再注册"（T5/D4 Q1）已经保证：**声明的瞬间
   consumer 一定存在**。网关因此无需"提前"创建，定向发布的持久性由 work-queue
   保留（消息在 stream 里等到 consumer fetch）保证。
3. 声明机制同时解决了"legacy worker 不能被定向"的判别问题（未声明 = 永不定向），
   比按版本号猜更直接。

失败面因此收敛为：worker 未绑定 → 不声明 → 走共享，与 T12 之前完全一致。

## 阈值与平局口径（对票面的一句话解释）

票面维度序 "affinity > load > locality；平局取低负载"。实现取
**affinity 阈值 1**：零重叠即无定向理由 → 走共享（overflow 严格不差，且不承担
"定向到错误 worker"的风险）。达标者之间按 affinity desc → load_percent asc →
locality desc 排序——`load` 排在 `locality` **之前**，即"平局取低负载"的落地；
最后以 worker_id asc 兜底保证确定性（HashMap 迭代序不稳定，没有兜底键会让
同分候选的定向结果在两次 tick 之间抖动）。

## 交付记录（实现细节与偏差）

- **`affinity_hits` 计"不同约束"而非出现次数**。分数同时用作排序键，调用方重复
  一个路径（或同时声明 `src/a.rs` 与 `./src/a.rs`）不该抬高它。单测
  `affinity_counts_distinct_constraints_only` 钉死该口径（首版按出现计，被该
  单测拦下后改为去重计数）。
- **声明是"每次心跳"的**：`WorkerRegistry::heartbeat()`（无信号的 legacy 形态）
  会清空 `recent_files` 并复位 `per_worker_topic=false`。也就是说停发声明的
  worker 立刻恢复不可定向，而不是被永久记住。`heartbeat()` 保留为 delegating
  wrapper，避免打断既有调用点与测试。
- **fetch loop 参数化**：`_subtask_fetch_loop` 拆为
  `_subtask_fetch_loop_for(pull_sub, label)`，共享与 per-worker 两条订阅复用同一
  实现（`label` 只用于日志）。`stop()` 现在先取消共享 loop、再取消 per-worker
  loop、最后才做在飞执行的快照清扫——顺序与共享 loop 一致，否则 per-worker loop
  会在清扫后继续派发新任务。
- **server 层无 NATS mock 夹具**（`nats_client` 是具体类型，无 trait 注入点），
  因此"定向 vs 共享"的判定在**自由函数 `resolve_dispatch_subject`** 与
  `registry.placement_target` 两层做单测，而不是通过一次真实 publish 断言
  subject。两层合起来覆盖了票面三条验收（重叠→定向、无重叠/过期→共享、
  legacy→仍经 overflow）。
- **范围外仍有硬编码共享 subject 的两处**：`uc-engine/src/scheduler/executor.rs`
  的 `NatsExecutor` 与 `scheduler/dispatcher.rs`——它们不是 gateway dispatch
  mouth，票面只要求网关侧定向；两处记录的定向留作后续（ExecutorSelector 路径）。
- **零搁死审计**：定向只是"先去哪里"。`resolve_dispatch_subject` 的 `None` 分支
  返回共享常量（正常结果，不是失败）；`publish_with_headers` 的失败/重试/回
  Pending 语义完全未改；定向**不写** `assigned_worker`，reaper 的 stale-Assigned
  语义不变。

