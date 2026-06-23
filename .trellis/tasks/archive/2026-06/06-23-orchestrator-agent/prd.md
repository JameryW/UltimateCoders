# 完善 Orchestrator 为完整 Agent：规划、检索、问答能力

## Goal

让 Orchestrator 从纯粹的"任务分解+调度器"升级为具备自主推理能力的 Agent——能通过 LLM tool-calling loop 自主检索代码库、搜索记忆、规划任务，并回答用户问题。

## Requirements

* Orchestrator 持有 LLMClient 实例
* Orchestrator 定义一组 tools：search_code、search_memory、codegraph_explore、read_file
* 新增 `plan_task()` 方法：LLM tool-calling loop 收集上下文后生成规划
* 新增 `ask()` 方法：LLM tool-calling loop 回答用户问题
* `decompose_task()` 增强为：先 plan_task 收集上下文，再分解（上下文注入分解 prompt）
* `_gather_memory_context` / `_gather_code_context` 实现为实际检索
* 上下文预算控制：tool 结果截断 2000 chars + token 预算 50K + 超预算时停止收集

## Acceptance Criteria

* [ ] Orchestrator 可通过 LLM tool-calling 自主搜索代码库
* [ ] `plan_task()` 返回带有代码上下文的任务规划
* [ ] `ask()` 能基于代码库回答问题
* [ ] `decompose_task()` 使用 plan_task 的上下文，提升分解质量
* [ ] 现有 test_agent.py 测试通过
* [ ] 新增能力的单元测试

## Definition of Done

* Tests added/updated
* Lint / typecheck green
* 不破坏现有 Orchestrator-Worker 流程

## Decision (ADR-lite)

**Context**: Orchestrator 的 LLM tool-calling 与现有 sandbox 分解的关系
**Decision**: 前置增强方案 — Orchestrator 先用 LLM tool-calling 收集上下文，注入 sandbox 分解 prompt。sandbox 仍负责最终分解。
**Consequences**: 不破坏现有流程；sandbox 的代码理解能力保留；上下文收集质量提升分解效果

**Context**: 上下文窗口溢出风险
**Decision**: 分层截断 + 预算控制 — tool 结果硬限 2000 chars + token 预算 50K + 超预算停止收集
**Consequences**: 防御性措施，实际 2-3 轮 tool-calling 约 6K tokens，远低于 200K 窗口

## Out of Scope

* Orchestrator 不执行代码修改（仍是 Worker 职责）
* 不修改 Rust 引擎层
* 不修改 Dashboard UI
* 不实现 streaming
* 不实现 LLM summarize 压缩（后续按需加）

## Technical Notes

* 关键文件：
  - `python/ultimate_coders/agent/orchestrator.py` — 主要修改目标
  - `python/ultimate_coders/agent/llm.py` — LLMClient + ToolDefinition + make_tool_definition
  - `python/ultimate_coders/agent/codegraph.py` — CodegraphClient (search/callers/callees/impact/explore)
  - `python/ultimate_coders/agent/types.py` — OrchestratorConfig 扩展
  - `python/ultimate_coders/search/query.py` — SearchQuery builder
  - `tests/python/test_agent.py` — 现有测试
* Tool 结果截断：codegraph.explore 已有 2000 chars 截断，search 结果需加
* Token 预算：OrchestratorConfig.planning_context_budget 默认 50000
