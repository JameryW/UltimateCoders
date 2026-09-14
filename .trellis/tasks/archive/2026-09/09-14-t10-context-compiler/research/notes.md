# T10 勘察笔记

## 数据流现状

- **提交输出唯一权威源**：`graph_store.commit_once` → `node_completions.result_ref`
  （Option<String>）。result_ref 的实值 = `PendingGraphVerb::Commit(subtask_update
  .result 或 subtask.result.summary)`（server.rs ~1480）→ `fanout_graph_commit` →
  sink.on_commit。即 result_ref 就是依赖节点的 result summary 字符串。
- **payload 构造唯一入口**：`server.rs::subtask_execute_payload(task_id, st,
  project_id, expected_output, file_constraints)` —— publish_ready_subtasks（~2710，
  expected_output=""）与 dispatch_ready_subtasks（~3756，带全量）两个发布嘴都走它。
- **worker 解析链**：`_parse_subtask_message` → `_build_subtask_from_data`
  （构造 Subtask，data 键直读）→ `_execute_and_report(subtask)` → `worker.
  execute_subtask(subtask)`（内部 `self._context_injector.build_context
  (subtask.depends_on)`，agent/worker.py ~695；输出为文本块，"+search_block"）。
- **执行上下文文本格式**（ContextInjector.build_context，state_sync.py ~154）：
  "## Context from completed subtasks\n" + "### Subtask {id[:8]} (✓)\n" +
  "Summary: {summary[:1000]}\n"，尾部超 4000 字符截断。渲染网关块时对齐此风格。

## 设计决策

- `ContextBlock/ContextEntry` 定义在 uc-types envelope.rs（wire 契约同文件；
  8 KiB = 8*1024，按 serde_json 序列化长度贪心装箱，entries 按 node_id 字典序
  先排序——确定性）。
- `ExecutionEnvelope.context_block: Option<ContextBlock>`（serde default +
  skip_serializing_if=None）+ `NatsSubtaskExecute.context_block` 顶层同字段
  （payload 平铺 envelope 字段族的既有形态）。两者在 subtask_execute_payload
  内同源赋值。
- GraphShadowSink 新默认方法 `committed_dep_outputs(graph_id, node_ids) ->
  Vec<ContextEntry>`（default 空 vec = fail-soft）；GraphStore 实现 LEFT JOIN
  node_completions；sink 覆写吞错降级空 vec（上下文缺失绝不 fail dispatch）。
- 组装点：两发布嘴内逐 st 组装（依赖非空才查）；无 graph shadow / 存储错 →
  context_block=None。
- Python 不动 Subtask dataclass（跨 pyo3 边界）：`execute_subtask(subtask,
  gateway_context_block=None)` 加参；`_execute_and_report` 加参透传；JS handler
  从 data["context_block"] 取。无键 → None → 回退 injector（行为不变）。
- 渲染函数放 agent/worker.py 模块级（便于 pytest 直测）。

## 测试面

- Rust 单测：compose（空→None、排序确定性、8KiB 截断+marker）、envelope
  golden（带/不带 context_block 往返 + "{}" 默认）、subtask_execute_payload
  落地字段。
- PG 集成（#[ignore]）：seed graph_nodes+node_completions → committed_dep_outputs
  返回 {success, summary}；FAILED 节点 success=false、无 completion 行 summary=""。
- pytest：渲染函数（成功/失败/截断/空）；worker 优先路径（spy _execute_in_sandbox
  捕获 context_block 参数）；回退路径（gateway_context_block=None → injector）。
