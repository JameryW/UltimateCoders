# Research: 执行信封与注册/心跳现状（T1 前置勘察，2026-09-11）

## 1. 派发 payload（gateway → `uc.subtask.execute`）

- 主发布器：`crates/uc-grpc/src/server.rs` `publish_ready_subtasks`（L2056），`serde_json` 序列化 `NatsSubtaskExecute`（L232–268）。
  现有顶层键：`message_id`（`{task}:{execute}:{subtask}:{ts_ms}`，L2117）、`task_id`、`subtask_id`、`description`、`expected_output`、`file_constraints`、`timeout_seconds`、`retry_count`(=dispatch_retry_count)、`dispatch_mode`、`required_capabilities`、`agent_config_json`、`steps`、`project_id`。除前四个外全部 `#[serde(default)]` → **加字段 additive-safe**。
- 第二发布器（legacy）：`crates/uc-engine/src/scheduler/dispatcher.rs` L199–204，`json!` 只含 `task_id/subtask_id/description/layer` —— **两个发布器都要带信封**。
- worker 结果回传：`uc.task.update`（`NatsTaskUpdate`，message_id `{task}:{event}:{subtask}:{ms}`），gateway `check_and_record_message_id`（server.rs L510）内存 TTL 去重。

## 2. 注册/心跳链路（contract_version 挂载点）

- proto：单文件 `crates/uc-grpc/proto/engine.proto`。`RegisterWorkerRequest`（L771–776）：`worker_id/capabilities/max_capacity/metadata(string JSON)`；`WorkerHeartbeatRequest`（L784–787）：`worker_id/current_load`。**全仓无 contract_version/epoch/attempt 概念**。
- gateway：`worker_service.rs` `WorkerRegistry::register`（L70）存 `RegisteredWorker{..., metadata: String, ...}`（L29–37）；`STALE_TIMEOUT_SECS=60`。
- Python：gRPC 注册走 `_register_with_gateway`（nats_worker.py:1945）→ `engine.py:1083` → PyO3 `crates/uc-python/src/engine.rs:1227` → `client.rs:244`；`_registration_metadata`（:1985，docstring 声明 "Keys are stable API"）。NATS heartbeat `publish_heartbeat`（:317）键 `consumer_id/timestamp` + `w_info`（`worker_id/capabilities/current_load/max_capacity/pending_subtask_count`），gateway `_handle_heartbeat`（:2660）`.get()` 宽容读。
- 结论：contract_version 需要 proto 新字段 + PyO3 客户端参数透传（Python 无生成桩，走 typed struct 链）。

## 3. 解析宽容性（决定"一刀切"的落点）

- `_parse_subtask_message`（nats_worker.py:2175）只要求 `task_id/subtask_id`，未知键忽略；`_build_subtask_from_data`（:2246）全 `.get()`。Rust 侧 `NatsSubtaskExecute` 无 `deny_unknown_fields`。**双向 additive**。
- 因此本票只加字段+握手；**worker 拒收无信封消息属 T4**（届时 worker 才是新契约强制方）。

## 4. TS 与测试面

- TS 不消费 `uc.subtask.execute`（仅 `control-signal-subscriber.ts` 订 `uc.task.event`）；WorkerService proto 纯加字段对 TS 桩 codegen additive——**本票预计无需重生成两份桩**（验收时若 dashboard `pnpm run build` 有变化则一并提交）。
- 受影响测试：`tests/python/test_nats_worker_helpers.py`（dispatch payload L271–318、`_registration_metadata` L1158–1171）、`test_nats_jetstream_subtask.py`、`test_sandbox.py` L980–1077、`test_workflow_orchestration.py` L584–614。
- TS subtask id 生成点（idempotency_key 恒等映射的输入）：`orchestrator.ts:223`（`st.id`/`st-${i+1}`）、`scheduler.ts:469/500`（拆分后缀）。

## 5. idempotency_key 规范（本票定义）

`key = sha256("{graph_id}:{node_id}:{attempt_id}")[:32]` —— 不含时间戳/随机数；同一派发重发消息键不变（这是与现 message_id 含 millis 的根本区别）。attempt 语义在 T3 前用 `dispatch_retry_count` 充当 retry_no。
