# Pipeline全链路优化: 执行效率+事件管道+反馈质量

## Goal

优化从任务提交到 TUI/Dashboard 展示的全链路性能和反馈质量，消除不必要的轮询延迟，提升事件实时性，增强失败诊断能力。

## Requirements

### PR1: Event-driven dispatch (取代2s轮询)

**现状**: `_auto_execute_loop()` 每2s轮询 ready subtask，即使前一个 subtask 已经完成也要等下一轮。

**目标**: subtask_completed/subtask_failed 事件直接触发下一轮 dispatch，无空闲等待。

- Python `SandboxTUI._auto_execute_loop`: 用 `asyncio.Event` 替代 `asyncio.sleep(2)`，在 `_listen_for_events()` 收到 subtask_completed/failed 时 set event
- Python `NatsWorker._execute_subtasks`: 同理，内层 `asyncio.sleep(0.5)` 改为 event-driven
- 保留安全超时（如30s无事件时仍触发检查，防止死锁）

### PR2: 事件优先级 flush

**现状**: `useTaskEvents` 的 `flushEventBuffer()` 以 50ms batch 处理所有事件，status 变迁和 tool_call 同等对待。

**目标**: status 变迁事件（subtask_assigned/started/completed/failed）立即 flush，其余事件继续 batch。

- `useTaskEvents.ts`: `processEvent()` 返回 priority 标志（high/low）
- stream.on('data') 回调中，high priority 事件直接 flush，low priority 走 batch timer
- 不改 proto，不改 Rust 端，纯前端优化

### PR3: 失败上下文增强

**现状**: `subtask_failed` 事件只携带 error message。

**目标**: 失败时附带 stderr tail + 失败前工具调用链摘要。

- Python `Worker.execute_subtask()`: 在 SubtaskResult 中增加 `stderr_tail` 和 `recent_tool_calls` 字段
- Python `Worker._publish_event()`: subtask_failed 的 data 中增加 `stderr_tail`、`recent_tools`
- Rust `apply_worker_event_to_store()`: SubtaskFailed 中增加 `stderr_tail`、`recent_tools` 到 data map
- TUI `processEvent()`: subtask_failed 时提取并展示 stderr_tail
- TUI `SubtaskDetail`: failed subtask 展示 stderr_tail 和 recent_tools

## Acceptance Criteria

- [ ] PR1: Local模式下，subtask完成后下一个 ready subtask 在 <500ms 内开始执行（当前 ~2s）
- [ ] PR1: NATS模式下，_execute_subtasks 内层等待从 0.5s 轮询变为 event-driven
- [ ] PR2: status 变迁事件（assigned/started/completed/failed）在 <50ms 内更新 TUI（当前 50ms batch 延迟）
- [ ] PR2: tool_call/file_modified 事件仍走 batch，不增加 TUI 渲染频率
- [ ] PR3: subtask_failed 事件包含 stderr_tail（最后10行）和 recent_tools（最后5次工具调用）
- [ ] PR3: TUI SubtaskDetail 展示 stderr_tail 和 recent_tools
- [ ] 所有现有测试通过
- [ ] 新增测试覆盖 event-driven dispatch 逻辑

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* 不改变 proto 定义（保持向后兼容）
* Local fallback 模式仍正常工作

## Out of Scope

* 增量拆分（需改 proto）
* 工具调用流式输出（需改 proto）
* 沙箱进程复用（改动太大）
* Dashboard 单流合并（需改 proto）
* 背压信号（低优先级）
* 统一 Worker 模式（架构重构）
* 事件存储统一（架构重构）
* Python SSE Dashboard 去除（后续迭代）

## Technical Approach

### PR1: Event-driven dispatch

**Python SandboxTUI** (`python/ultimate_coders/tui/app.py`):
1. 添加 `self._dispatch_event = asyncio.Event()` 到 `__init__`
2. `_listen_for_events()`: subtask_completed/subtask_failed 时调用 `self._dispatch_event.set()`
3. `_auto_execute_loop()`: 用 `await self._dispatch_event.wait()` 替代 `await asyncio.sleep(2)`
4. `_dispatch_event.set()` 后立即 clear，配合 30s 安全超时

**Python NatsWorker** (`python/ultimate_coders/nats_worker.py`):
1. 添加 `self._dispatch_event = asyncio.Event()` 到 `__init__`
2. `_execute_subtasks()` 内层 `asyncio.sleep(0.5)` → `self._dispatch_event.wait()` + 30s timeout
3. Worker._publish_event → NATS → NatsWorker 消费 uc.task.event → set dispatch_event

### PR2: 事件优先级 flush

**TUI useTaskEvents** (`tui/src/hooks/useTaskEvents.ts`):
1. 定义 `HIGH_PRIORITY_EVENTS = new Set(['subtask_assigned','subtask_started','subtask_completed','subtask_failed','task_completed','task_failed'])`
2. `processEvent()` 返回 `{ updated, priority: 'high' | 'low' }`
3. stream.on('data') 回调: high priority → 直接 `setSubtaskMap` + `setEvents`（绕过 buffer）; low priority → 走 buffer + batch timer

### PR3: 失败上下文增强

**Python Worker** (`python/ultimate_coders/agent/worker.py`):
1. `SubtaskResult` 增加 `stderr_tail: str = ""` 和 `recent_tool_calls: list[str] = field(default_factory=list)`
2. `_execute_in_sandbox()`: 从 sandbox output 提取 stderr tail
3. `_publish_event()` subtask_failed: data 增加 `stderr_tail` 和 `recent_tools`

**Rust apply_worker_event_to_store** (`crates/uc-grpc/src/server.rs`):
1. SubtaskFailed 分支: 从 `worker_event.data` 提取 `stderr_tail` 和 `recent_tools`，存入 AgentEventType::SubtaskFailed 的 data

**TUI processEvent** (`tui/src/hooks/useTaskEvents.ts`):
1. subtask_failed 分支: 提取 `stderr_tail` 和 `recent_tools`，存入 SubtaskItem
2. SubtaskDetail 组件: 展示 stderr_tail（红色）和 recent_tools（dim）

## Decision (ADR-lite)

**Context**: 4大类12项优化太多，需分优先级
**Decision**: MVP做 PR1+PR2+PR3（高ROI、中低难度、无外部依赖），其余放后续迭代
**Consequences**: 不改proto，保持向后兼容，但流式输出和增量拆分等高影响优化推迟

## Technical Notes

* PR1 改动文件: `python/ultimate_coders/tui/app.py`, `python/ultimate_coders/nats_worker.py`
* PR2 改动文件: `tui/src/hooks/useTaskEvents.ts`
* PR3 改动文件: `python/ultimate_coders/agent/worker.py`, `python/ultimate_coders/agent/types.py`, `crates/uc-grpc/src/server.rs`, `tui/src/hooks/useTaskEvents.ts`, `tui/src/components/SubtaskTree.tsx`
* 不改 proto 文件，不改 gRPC 服务定义
