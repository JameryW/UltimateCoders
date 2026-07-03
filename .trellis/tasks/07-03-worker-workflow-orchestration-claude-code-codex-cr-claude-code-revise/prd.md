# worker workflow orchestration: claude-code→codex-cr→claude-code revise

## Goal

让 subtask 内部支持多 agent 步骤链式编排。典型场景：claude-code 写代码 → codex 做 code review → codex 的 CR 反馈喂回 claude-code 继续修改。当前一个 subtask = 单 agent 单次 sandbox 执行，无法表达 subtask 内的 agent 间协作。

## What I already know

- Subtask 结构 (`crates/uc-types/src/agent.rs`): 有 `agent_config_json` 覆盖**单个** agent 配置，无 step 概念。
- 两个 agent adapter 已存在: `crates/uc-engine/src/sandbox/agents/{claude_code,codex}.rs`，各自 build ExecRequest + parse AgentOutput。
- AgentOutput: `summary` + `file_changes` + `token_usage` + `success`。codex 输出 plain text，claude-code 输出 JSON（含 cost/turns）。
- Subtask 间已有 `depends_on` 编排（subtask 级），但本次需求是 **subtask 内** agent 间编排。
- Python worker (`python/ultimate_coders/agent/worker.py`) 调 SandboxManager 跑单 agent，解析 stdout。
- Decomposer (`packages/uc-orchestrator/src/agents/decomposer.md`) 生成 subtask 列表，当前不产 step。
- `agent_config_json` 仅 Rust sandbox 侧消费；Python worker 侧 `SubtaskAgentConfig` 镜像。

## Assumptions (temporary)

- 编排在 worker 本地（subtask 派给某 worker 后，worker 内顺序跑 step），不跨 worker。
- step 间共享同一 workspace/worktree（同一 subtask 的 step 不重新 acquire worktree）。
- 失败 step 可终止链或继续（待定策略）。

## Decisions (locked)

- **D1: step 链放 uc-types Subtask 层**。加 `steps: Vec<Step>` 字段。空 = 单步 fallback 到现有 `agent_config_json`。类型安全 + decomposer 产出 + OMP UI 可见 + 向后兼容。

## Open Questions

- step 间上下文如何传递（前步 stdout/summary 注入下步 prompt）？
- MVP 是否支持条件跳转（CR 通过则跳过 revise 步）还是纯线性？
- step 内 prompt 模板变量语法（{{prev.summary}} / {{steps[0].output}}）？

## Requirements (evolving)

- subtask 可声明有序 step 列表，每 step 指定 agent + prompt。
- step 按序执行，前步输出可被后续 step 引用。
- 至少支持 claude-code→codex→claude-code 三步链。

## Acceptance Criteria

- [x] 能在 subtask 配置里声明 3-step 链并跑通（test_execute_steps_runs_chain_in_order_and_threads_output）。
- [x] 第 3 步 claude-code 能拿到第 2 步 codex 的 CR 输出（{{prev_summary}} 注入，测试覆盖）。
- [x] 单 step subtask 行为不变（向后兼容，steps 空 = 旧路径，544 旧测试 0 回归）。
- [x] 失败 step 默认终止链；abort_on_failure=False 可继续（两测试覆盖）。
- [x] file_changes 跨步累积（test_execute_steps_accumulates_file_changes_across_chain）。

## Definition of Done (team quality bar)

- [x] Tests added: tests/python/test_workflow_orchestration.py (11 tests)
- [x] cargo check --workspace / cargo test --lib green (344+114+7 passed)
- [x] pytest green (544 passed, 0 fail)
- [x] subtask schema 变化已贯穿 uc-types → proto → conversions → NATS payload → Python Subtask

## Out of Scope (explicit)

- 跨 worker 编排（step 不跨 worker）
- step 间并行（线性 only）
- 动态 agent 选择（MVP 固定 step 列表）
- **decomposer 自动产 steps**：OMP decomposer.md 当前 schema 不含 steps，手动声明 steps 已可用。decomposer 自动生成是后续 PR。
- step 级 agent_config_json (Rust string) ↔ Python agent_config (dict) 的字段名 mismatch 沿用既有 agent_config 同样问题，MVP 不修（step 用 agent+prompt 即可工作）。

## Technical Approach

数据流:
1. `uc-types::WorkflowStep` struct + `Subtask.steps: Vec<WorkflowStep>` (空 = 旧单 agent 路径)
2. proto `WorkflowStepProto` + `SubtaskProto.steps` → conversions 双向映射
3. `NatsSubtaskExecute.steps` → NATS dispatch payload 携带 steps 到 worker
4. Python `WorkflowStep` dataclass + `Subtask.steps` + to_dict/from_dict
5. `nats_worker._handle_subtask_execute` 解析 steps 入 Subtask
6. `worker._execute_in_sandbox`: steps 非空 → `_execute_steps` 链式驱动；空 → 旧路径
7. `_execute_steps`: 按 step.agent 调 `SandboxManager.execute(..., agent=step.agent)`，`_render_step_prompt` 渲染 `{{prev_summary}}`/`{{prev_files}}`/`{{stepN.*}}`/`{{context}}`/`{{file_constraints}}`，file_changes 跨步累积，失败按 abort_on_failure 决定终止/继续
8. `SandboxManager.execute` 加 `agent` 参数覆盖 adapter（复用 pool，切 adapter）

## Decision (ADR-lite)

**Context**: subtask 内需多 agent 链（claude-code 写→codex CR→claude-code 改），现有单 agent sandbox 执行无法表达。
**Decision**: Subtask 加 `steps: Vec<WorkflowStep>` 一等字段（非藏 JSON），空 = 向后兼容单步。SandboxManager.execute 加 per-call agent 覆盖，worker 层 _execute_steps 驱动链。prompt 模板用简单 string replace（零依赖），支持 prev_summary/prev_files/stepN.*/context/file_constraints。
**Consequences**: 类型安全 + UI 可见 + decomposer 可自然扩展。模板语法简单（无循环/条件），复杂控制流需后续。step 级 agent_config 字段名 mismatch 待统一。

## Technical Notes

- 关键文件: `crates/uc-types/src/agent.rs` (Subtask), `crates/uc-engine/src/sandbox/agents/mod.rs` (adapter), `python/ultimate_coders/agent/worker.py` (执行)
- decomposer 需扩展产出 step 字段
