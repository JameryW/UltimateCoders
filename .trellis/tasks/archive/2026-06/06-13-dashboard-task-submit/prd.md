# Dashboard 提交代码任务 + 实时追踪

## Goal

在 Dashboard 上新增"提交代码开发任务"入口，用户输入任务描述提交给 Orchestrator，实时查看任务执行进度、Worker 与 LLM 的交互内容（请求/工具调用/响应）、以及最终产出（修改的文件、代码变更）。

## Requirements

### 任务提交

1. **提交表单** — Dashboard 顶部新增任务提交区域：任务描述（textarea）+ project_id（可选 text input）+ 提交按钮
2. **POST 端点** — `POST /dashboard/api/tasks/submit` 接受 `{description, project_id?}`，调用 `orchestrator.submit_task()`
3. **提交后反馈** — 提交成功显示 toast + 自动滚动到任务列表

### 事件总线（TaskEventEmitter）

4. **TaskEventEmitter** — Orchestrator 持有的事件发射器，Worker 通过它 emit 实时事件
5. **事件类型**：
   - `task_submitted` — 任务提交
   - `subtask_started` — subtask 开始执行
   - `llm_request` — LLM 请求发送（含 model, messages 摘要）
   - `tool_call` — 工具调用（含 tool name, input）
   - `tool_result` — 工具返回（含 result 摘要）
   - `subtask_completed` — subtask 完成（含 summary, success）
   - `subtask_failed` — subtask 失败
   - `task_completed` — 整体任务完成
6. **事件结构** — `{"timestamp", "type", "task_id", "subtask_id?", "data": {...}}`
7. **Dashboard 订阅** — DashboardApp 持有 emitter 引用，事件写入 `_event_log` deque

### SSE 混合推送

8. **即时事件推送** — 每个事件通过 SSE 立即推送（event type: `task_event`），不等 5s 周期
9. **保留 5s 全量快照** — `update` 事件继续每 5s 推送全量状态
10. **前端事件处理** — 收到 `task_event` 时追加到交互日志面板，不重建整个 UI

### 任务详情面板

11. **任务详情展开** — 点击任务行展开详情面板，显示：
    - Subtask 列表 + 状态 + Mermaid DAG
    - 交互日志流（按时间排列的 LLM/tool 事件）
    - 产出文件列表（从 SubtaskResult.modified_files 获取）
12. **交互日志过滤** — 可按 subtask 筛选交互日志
13. **产出内容查看** — 文件变更显示 diff 或文件路径列表

### Worker 集成

14. **Worker 回调** — Worker.execute_subtask() 增加 `event_emitter` 参数，在关键节点 emit 事件
15. **LLMClient 钩子** — complete_with_tools() 增加 `on_tool_call` 回调参数，每轮 tool 调用时触发
16. **向后兼容** — event_emitter 和回调均为可选，不传时行为不变

## Acceptance Criteria

- [ ] POST /dashboard/api/tasks/submit 可提交任务，返回 task_id
- [ ] 提交后 SSE 立即推送 task_submitted 事件
- [ ] Worker 执行 subtask 时通过 emitter 推送 subtask_started/completed 事件
- [ ] LLM tool 调用时推送 tool_call/tool_result 事件
- [ ] 前端交互日志面板实时显示事件流
- [ ] 任务详情展开显示 subtask 列表 + 交互日志 + 产出文件
- [ ] 无 emitter 时 Worker 行为不变（向后兼容）
- [ ] 新增端点有对应测试
- [ ] Lint / CI green

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* Docs/notes updated

## Decision (ADR-lite)

**Context**: Dashboard 需要提交任务并实时追踪 Worker 执行过程，包括 LLM 交互和工具调用。

**Decision**:
- 引入 TaskEventEmitter 事件总线，Worker 通过它 emit 实时事件
- SSE 混合推送：5s 全量快照 + 即时事件推送
- 任务提交表单最小集：description + project_id（可选）
- LLMClient.complete_with_tools 增加 on_tool_call 回调

**Consequences**:
- Worker 和 LLMClient API 变更（新增可选参数，向后兼容）
- SSE 流量增加（每个 tool call 一个事件），但单事件体量小
- 事件不持久化，重启丢失

## Out of Scope

* 事件持久化到数据库（future work）
* 任务取消功能（后续任务）
* 流式 LLM 响应（token-by-token，future work）
* 多 Orchestrator 聚合

## Technical Notes

* Worker.execute_subtask() 需增加 event_emitter 参数
* LLMClient.complete_with_tools() 需增加 on_tool_call 回调
* DashboardApp._setup_routes() 新增 POST submit 端点
* SSE event_generator 需改为同时监听 5s 定时器 + 事件队列
* 前端 dashboard.js 新增任务提交表单 + 交互日志面板 + 产出文件列表
* TaskEventEmitter 用 asyncio.Queue 实现（线程安全，async 可 await）
