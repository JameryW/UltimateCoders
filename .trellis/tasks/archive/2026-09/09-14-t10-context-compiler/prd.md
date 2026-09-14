# T10: Context Compiler — envelope context_block + 网关组装 + worker 优先消费 (#652)

## 背景

D10 #647：依赖上下文组装从 worker 上移到 Rust 网关。今天 worker 在执行期用
`_context_injector.build_context(depends_on)`（从自身 Orchestrator 状态）重建依赖
摘要——权威翻转后残留的第二事实源；worker-only 模式（无 Orchestrator 状态）完全
丢失依赖上下文。图平面的 `node_completions.result_ref` 已存每个节点的提交输出，
只有网关看得到。

## Scope

1. **envelope 加性字段**：`ExecutionEnvelope.context_block: Option<ContextBlock>`
   （serde default + skip_serializing_if，无 contract_version bump；legacy worker
   忽略未知键——同 T5 effect_class 路径）；dispatch payload `NatsSubtaskExecute`
   同步加顶层 `context_block`（serde default）。envelope golden 加一例。
   `ContextBlock { entries: Vec<ContextEntry>, truncated: bool }`，
   `ContextEntry { node_id, success, summary }`。
2. **网关组装**：`subtask_execute_payload` 唯一构造点加 context_block 参数；
   `publish_ready_subtasks` / `dispatch_ready_subtasks` 两个发布嘴在发布前组装：
   depth-1 依赖（`st.depends_on`），`{node_id, success, summary}` ** exclusively**
   来自图平面已提交输出（新 GraphShadowSink 读方法 `committed_dep_outputs`，
   GraphStore 实现：graph_nodes LEFT JOIN node_completions，success = state
   SUCCEEDED，summary = result_ref）；entries 按 node_id 字典序；序列化总量
   8 KiB 封顶，溢出即丢后续 entry 并打 `truncated=true`；无依赖或图平面不可用
   → context_block 缺席（绝不 fail dispatch）。
3. **worker 消费**：`execute_subtask` 优先用网关 context_block（渲染成与
   ContextInjector 同风格文本）；缺席时回退 `_context_injector.build_context`
   （in-flight 消息、旧网关——行为与今天完全一致）。`_build_search_context`
   （codegraph，worker 侧）不变。
4. **测试**：compose 单测（空依赖/溢出截断/确定性排序）；envelope golden；
   payload 落地测试；pytest worker 优先路径 + 回退路径；PG 集成
   committed_dep_outputs（#[ignore]，并入欠账）。

## 验收（票面）

- 被派发节点的 envelope 携带其依赖的已提交摘要，worker 无需读 Orchestrator
  状态（worker-only 模式获得依赖上下文）。
- 缺 context_block → worker 行为与今天一致（回退）。
- >8 KiB 上下文 → 截断块 + marker，绝不 fail dispatch。

## 非目标

- codegraph search（worker 侧，不变）；context_block 历史/审计；
  TS/Dashboard 消费。
