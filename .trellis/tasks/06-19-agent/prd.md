# 完善Agent能力实现

## Goal

完善 Python Agent 层的 Worker 和 Orchestrator，使 agent 具备生产可用的自主执行能力：工具链补全、自适应决策、自省反馈、并发调度、测试覆盖。

## What I already know

* PR14 分支已有基础实现（commit 5efdbbb）：14 个工具、auto-retry、re-decompose、_self_evaluate、NatsWorker 并发
* Worker 工具集完整但 schema 粗糙：search、read_memory、write_memory、edit_file、search_memory、read_file、list_files、symbol_search、find_callers、find_callees、impact_analysis、explore_code、run_command、apply_diff
* Orchestrator：decompose_task（LLM + sandbox 双路径）、auto-retry（max_retries=3）、re-decompose failed、night window exclusive、NATS 事件
* LLMClient：anthropic 原生 + litellm 多 provider、tool-calling loop、streaming、retry with backoff
* 测试：77 pass，coverage 28%（worker 45%、orchestrator 0%）

## Requirements

### R1: 工具定义精度
* 每个 Worker 工具的 description 精确描述功能、适用场景、返回格式
* input_schema 包含所有参数的 type + description + required 标记
* 枚举型参数（如 key_scope、scope_type）使用 enum 约束

### R2: Orchestrator 并发调度
* 新增 `schedule_subtasks()` 方法：分析 DAG，将无依赖的 subtask 并发分配
* 基于 Worker 负载和 capabilities 做最优分配
* 并发执行结果收集 + 依赖就绪检查 → 触发下一轮调度

### R3: 自适应决策
* Worker 执行失败时根据错误类型差异化处理：
  - timeout → 缩小范围（减少 max_tokens/timeout）重试
  - tool_not_found → 降级到替代工具
  - engine_error → 跳过工具，纯 LLM 推理
  - conflict_detected → 等待并重试
* 在 SubtaskResult 中记录 adaptation_strategy

### R4: 自省反馈闭环
* _self_evaluate 输出影响后续决策：
  - 低置信度（<0.5）→ 触发 re-decompose
  - 中等置信度（0.5-0.7）→ 增加验证步骤
  - 高置信度 → 正常完成
* 执行经验写入 memory，后续 subtask 可参考

### R5: 测试覆盖
* Orchestrator 测试：decompose、concurrent schedule、re-decompose、night window
* Worker 自适应测试：各类错误场景的差异化处理
* 工具定义测试：schema 完整性校验
* 总测试 ≥ 90，CI green

## Acceptance Criteria

- [ ] 所有 14 个 Worker 工具的 description + input_schema 完整且精确（枚举参数有 enum）
- [ ] Orchestrator.schedule_subtasks() 可并发调度无依赖 subtask 到多个 Worker
- [ ] Worker 执行失败时根据错误类型自适应（4 种策略）
- [ ] _self_evaluate 低置信度触发 re-decompose，经验写入 memory
- [ ] 新增测试覆盖上述能力，总测试 ≥ 90
- [ ] ruff lint + cargo check/clippy + CI green

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* Worker coverage > 60%, Orchestrator coverage > 50%

## Decision (ADR-lite)

**Context**: 5 个方向都需要，全部纳入本轮 MVP
**Decision**: 按 R1→R3→R4→R2→R5 顺序实现（工具定义是基础，自适应和自省依赖它，并发调度独立，测试最后）
**Consequences**: PR 可能较大，但每个 R 独立可测

## Out of Scope

* Rust 侧改动
* Sandbox mode 扩展
* 新 LLM provider 接入
* Dashboard/TUI 前端改动

## Technical Notes

* worker.py: 1615 行，45% coverage — 工具定义在 _build_tool_definitions()，执行在 _execute_tool()
* orchestrator.py: 1389 行，0% coverage — 缺 schedule_subtasks() 并发入口
* llm.py: 777 行 — ToolDefinition/input_schema 格式已确定
* types.py: Subtask/Task/SubtaskResult 等核心类型 — 可能需扩展 adaptation_strategy 字段
