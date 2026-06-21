# 优化任务执行链路：删 LLM 模式、精简分解检索、事件去重、双写合并、Worker 独立进程

## Goal

精简 Python Agent 层架构：删除 Worker 的 LLM tool-calling 模式（只保留 sandbox），简化 Orchestrator 分解时的过度检索，统一事件管道，增加 NATS 消息去重幂等。

## What I already know

- Worker 有两条执行路径：`_execute_with_llm()`（Python tool-calling 循环）和 `_execute_in_sandbox()`（调 claude/codex CLI）
- Sandbox 模式的 coding agent（Claude Code）自己有完整工具链，不需要 Worker 包装 tool-calling
- NatsWorker 默认 `execution_mode="llm"`，只有 `sandbox_mode="subprocess"` 才走 sandbox
- Orchestrator.decompose_task() 先调 `_gather_memory_context()` + `_gather_code_context()` 搜代码片段塞进 prompt，但 Claude Code 分解时自己能读文件
- 事件双写：每个状态变更同时走 nats_publisher + event_emitter
- NATS at-least-once 语义可能重复投递，Rust 端 apply_update/record_event 无去重
- Orchestrator + Worker 同进程，sandbox subprocess 执行可能阻塞 asyncio 事件循环

## Requirements

### R1: 删除 Worker LLM 模式，只保留 sandbox

- 删除 `Worker._execute_with_llm()` 及其依赖：`_build_messages`, `_execute_tool`, `_tool_definitions`, `_gather_prior_context`（LLM 版），`_self_evaluate`, `_collect_modified_files`, `_record_experience`, `_adaptive_retry`, tools 字典
- Worker.__init__ 删除 `llm_client`, `execution_mode` 参数，强制 sandbox
- Worker.execute_subtask() 直接调用 `_execute_in_sandbox()`，无条件分支
- NatsWorker._init_components() 删除 `sandbox_mode` 条件分支，始终用 sandbox 配置创建 Worker
- LocalWorker 同步修改
- Orchestrator.decompose_task() 删除 LLM 分支，只保留 sandbox 路径
- Orchestrator.__init__ 删除 `llm_client` 参数
- 删除或标记废弃 `agent/llm.py` 中的 tool-calling 相关代码（LLMClient.complete_with_tools 等）— 保留 LLMClient 本身用于未来可能的轻量调用
- 删除 Orchestrator 中的 `rate_limiter` 和 `circuit_breaker`（专为 LLM 调用设计）

### R2: 精简 Orchestrator 分解时的检索

- `_gather_memory_context()` 改为只返回项目结构摘要（文件树 top-level），不搜代码
- `_gather_code_context()` 删除或改为只返回模块依赖图（不搜代码片段）
- 分解 prompt 模板简化：去掉 memory_context/code_context 占位符，只保留 description + project_id
- Sandbox agent (Claude Code) 执行分解时自己会读文件，不需要预注入

### R3: 事件管道去重幂等

- NATS 消息加 `message_id` 字段（格式：`{task_id}:{event_type}:{subtask_id}:{timestamp_ms}`）
- Rust TaskStore.apply_update() 加 message_id 去重（HashMap<String, Instant>，TTL 5min）
- Rust TaskStore.record_event() 加 event dedup（同 message_id 跳过）
- TUI processEvent() 加幂等：相同 subtask_id + 相同 status + 相近 timestamp → 跳过

### R4: 双写合并 — 统一为 NATS 单一事件源

- 删除 Python TaskEventEmitter 的独立 event queue
- Dashboard SSE 改为从 NATS `uc.task.event` 订阅（NATS push consumer + callback → SSE yield）
- 保留 TaskEventEmitter 的 ring buffer（用于 REST API 查询），但不再有独立 queue
- ForwardingEventEmitter（LocalWorker）简化：只写 JSON-RPC，不再维护内部 queue
- Python 端所有事件只走 NatsPublisher，不再同时调 event_emitter.emit()

### R5: Worker 独立进程（本轮 out of scope，预留接口）

- Worker.execute_subtask() 中的 sandbox 调用改用 `asyncio.to_thread()` 包一层，避免阻塞事件循环
- 为未来 Worker 独立进程预留：_execute_in_sandbox 返回值和接口保持稳定

## Acceptance Criteria

- [ ] `grep -r "execution_mode" python/` 无结果（LLM/sandbox 分支彻底删除）
- [ ] `grep -r "_execute_with_llm" python/` 无结果
- [ ] Orchestrator 不接受 llm_client 参数
- [ ] 分解 prompt 不包含 memory_context/code_context 代码片段
- [ ] NATS 消息包含 message_id 字段
- [ ] Rust TaskStore 对重复 message_id 的消息幂等处理
- [ ] TUI 对重复事件幂等处理
- [ ] Python 端不再双写 event_emitter + nats_publisher
- [ ] Dashboard SSE 从 NATS 消费事件
- [ ] sandbox 调用不阻塞 asyncio 事件循环
- [ ] `python -m pytest tests/python/` 通过
- [ ] `cargo test -p uc-grpc` 通过

## Definition of Done

- Tests added/updated（修改的每个模块有对应测试）
- Lint / typecheck / CI green
- 删除代码 > 新增代码（这是减法任务）

## Out of Scope

- R5 Worker 完全独立进程（本轮只做 asyncio.to_thread 包装）
- Dashboard gRPC 模式（当前 feature branch 的 useDashboardGrpc.ts 另行处理）
- Rust 端重构（本轮只改 Python + Rust 去重逻辑）

## Technical Notes

### 主要修改文件

- `python/ultimate_coders/agent/worker.py` — 删除 LLM 模式，简化为 sandbox-only
- `python/ultimate_coders/agent/orchestrator.py` — 删除 llm_client/LLM 分支，精简检索
- `python/ultimate_coders/nats_worker.py` — 删除 execution_mode 条件，sandbox-only
- `python/ultimate_coders/local_worker.py` — 删除 LLM 相关逻辑
- `python/ultimate_coders/nats_worker.py` (NatsPublisher) — 加 message_id
- `python/ultimate_coders/agent/event_emitter.py` — 删除独立 queue，保留 ring buffer
- `python/ultimate_coders/dashboard/app.py` — SSE 从 NATS 消费
- `crates/uc-grpc/src/server.rs` — apply_update/record_event 加去重
- `tui/src/hooks/useTaskEvents.ts` — processEvent 加幂等

### 可安全删除的代码

- Worker: ~400 行 LLM tool-calling 循环（_execute_with_llm, _build_messages, _execute_tool, _self_evaluate, _adaptive_retry, _record_experience, _collect_modified_files, tools 字典）
- Orchestrator: _gather_memory_context 全文检索, _gather_code_context 全文检索, LLM 分支
- Orchestrator: rate_limiter, circuit_breaker 初始化和使用

### 保留的代码

- LLMClient 类本身（未来可能用于轻量调用，不删）
- DecomposeAdapter + parse_decomposition_output（sandbox 分解路径）
- ClaudeCodeAgent / CodexAdapter（Rust sandbox agents）
