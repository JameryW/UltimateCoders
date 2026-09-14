# T9 勘察记录 (2026-09-14)

## 现状

- **key 派生族**：`uc_types::ExecutionEnvelope::derive_idempotency_key` =
  `sha256("{a}:{b}:{c}").hex[:32]`（envelope.rs，IDEMPOTENCY_KEY_LEN=32）。
  Python 侧无 Rust 调用，靠 golden 向量钉死（test_nats_worker_helpers.py
  `test_execution_envelope_matches_rust_golden`，`394e2a22…`）。
- **图静止条件**：graph_nodes.state ∈ {SUCCEEDED, FAILED, CANCELLED} 即 terminal；
  静止 = 无任何非 terminal 行。`node_completions(node_id PK, graph_id,
  winning_attempt_id, result_ref)` 是 commit-once 输出载体 → output sha 从
  result_ref 派生（NULL 补空串）。
- **网关 graph 句柄**：`GrpcServerInner.graph_shadow: Option<Arc<dyn
  GraphShadowSink>>`（server.rs:506）；trait 在 uc-engine graph_store.rs:479，
  全部新动词走 default no-op（现有 impl——存储后端 + 测试 RecordingGraphSink
  ——不破坏）。
- **Python arbiter**：`orchestrator._schedule_arbitration(task)`（branches =
  `uc/subtask/{id[:12]}`）→ `_arbitrate_task(task_id, branches)` →
  `merge_arbiter.arbitrate(branches)` → log。fire-and-forget，无 key。
  Orchestrator 无 Engine 句柄 → 新增 opt-in `merge_gate`（nats_worker 接线
  grpc Engine 的包装）。
- **uc-python/client 链**：client.rs（tonic client）→ uc-python engine.rs
  （pyo3 async wrapper）→ engine.py（Engine 类）→ 调用方。

## 裁决

- 新类型进 `uc_types`（新模块 `merge.rs`）：`MergeGrantDecision`、
  `MergeOutcomeReport`、`MergeReportDecision`、`derive_merge_idempotency_key`、
  `sha256_hex`（全 64 hex 辅助）。uc-engine trait 与存储 impl、uc-grpc proto
  conversions 全用它们，避免 uc-engine 反向依赖 uc-grpc。
- `GraphShadowSink` 新动词（default fail-closed）：
  `issue_merge_grant(graph_id) -> MergeGrantDecision`（default granted=false,
  error "sink does not support merge grants"）、
  `report_merge_outcome(graph_id, key, &MergeOutcomeReport) -> MergeReportDecision`
  （default accepted=false）。
- 存储实现三查一写：
  1. 静止：`SELECT state, COUNT(*) … GROUP BY state`，任一非 terminal → granted=false；
  2. SUCCEEDED 集与输出：`graph_nodes ⋈ node_completions`（state='SUCCEEDED'）；
  3. 当前授权行：同 key+consumed → granted+replay；同 key 未 consumed →
     granted（崩溃恢复照常合并）；不同 key → UPSERT 重置（granted, replay=false）。
  report：无行/key 不符 → accepted=false；匹配未消费 → UPDATE consumed+outcome，
  accepted=true；匹配已消费 → accepted=true+replay=true（不写）。
- proto 生成：Rust build.rs 自动；TS 不消费（proto3 加性字段不破坏既有桩）。
- golden 向量：graph_id="g-1"，succeeded=[("n-1","out-1"),("n-2","out-2")]，
  preimage = `merge:g-1:n-1,n-2:n-1=sha256(out-1)hex;n-2=sha256(out-2)hex`。
