# Dashboard 功能完善

## Goal

在已有的 FastAPI + SSE 监控 Dashboard 基础上，增加交互运维操作和关键可视化能力，使其从"只读状态展示"升级为"可交互运维控制台"。

## Requirements

### 交互运维 API

1. **暂停/恢复任务** — Orchestrator 新增 `pause_task(task_id)` / `resume_task(task_id)` 方法，Dashboard 暴露 POST 端点
2. **重置 Circuit Breaker** — CircuitBreaker 新增 `reset()` 方法，Dashboard 暴露 POST 端点
3. **手动触发调度任务** — Scheduler 侧新增 `trigger_job(job_id)` 方法（或等效），Dashboard 暴露 POST 端点
4. **刷新 pending 任务** — 暴露已有 `flush_pending_tasks()` 为 POST 端点

### 可视化增强

5. **事件日志流** — DashboardApp 维护内存环形缓冲区（deque, maxlen=200），Orchestrator 状态变化时 append 事件，SSE 随快照推送，前端新增 Event Log 面板
6. **任务详情展开** — 点击任务行展开 subtask 列表 + 依赖 DAG，使用 Mermaid.js CDN 渲染
7. **操作确认弹窗** — 所有 POST 操作使用自定义暗色主题模态框确认，不使用浏览器原生 confirm

### 前端架构

8. 保持 Tailwind CDN + vanilla JS，无 npm/node 构建步骤
9. Mermaid.js 通过 CDN 加载（类似 Tailwind 的引入方式）
10. SSE 推送内容扩展：快照中新增 `events` 字段（最近 N 条事件）

## Acceptance Criteria

- [ ] POST `/dashboard/api/tasks/{id}/pause` 可暂停任务，返回 200
- [ ] POST `/dashboard/api/tasks/{id}/resume` 可恢复任务，返回 200
- [ ] POST `/dashboard/api/circuit-breaker/reset` 可重置 CB，返回 200
- [ ] POST `/dashboard/api/scheduler/jobs/{id}/trigger` 可触发调度任务，返回 200
- [ ] POST `/dashboard/api/tasks/flush-pending` 可刷新 pending 队列，返回 200
- [ ] 所有 POST 操作前弹出自定义确认模态框
- [ ] 操作失败时前端显示 toast 错误提示
- [ ] Event Log 面板显示最近事件，新事件出现在顶部
- [ ] 任务行可点击展开，subtask 列表 + Mermaid DAG 正确渲染
- [ ] SSE 快照包含 `events` 字段
- [ ] 现有测试通过，新增端点有对应测试
- [ ] Lint / typecheck / CI green

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes
* Rollback considered: POST 操作均为 Orchestrator 已有/新增方法调用，无破坏性变更

## Decision (ADR-lite)

**Context**: Dashboard 需从只读升级为可交互，需补齐 Orchestrator/CB/Scheduler 的操作方法，并选择 DAG 可视化方案和确认机制。

**Decision**:
- 补齐 Orchestrator (pause/resume)、CircuitBreaker (reset)、Scheduler (trigger_job) 方法后暴露 POST 端点
- 事件日志用内存环形缓冲区（deque, maxlen=200），不依赖外部存储
- DAG 渲染用 Mermaid.js CDN
- 操作确认用自定义暗色主题模态框

**Consequences**: 
- 增加少量 Python 方法代码，无 Rust 侧改动
- 事件日志重启丢失，可接受
- Mermaid CDN 增加约 500KB 前端资源，与 Tailwind CDN 策略一致
- 模态框需要手写 JS 组件，但保持无构建步骤架构

## Out of Scope

* 认证/访问控制（后续任务）
* WebSocket 替代 SSE（当前 SSE 满足需求）
* 历史时序图表（Chart.js 等，future work）
* Worker 级别操作（重启 worker、修改 capacity 等）
* 多 Orchestrator 实例的 Dashboard 聚合

## Technical Notes

* Dashboard 文件布局：`python/ultimate_coders/dashboard/{app.py, templates/index.html, static/dashboard.js}`
* Spec 文件：`.trellis/spec/backend/dashboard-spec.md`
* Orchestrator：`python/ultimate_coders/agent/orchestrator.py`
* Rate limiter / Circuit breaker：`python/ultimate_coders/agent/rate_limiter.py`
* Agent types：`python/ultimate_coders/agent/types.py`
* Mermaid.js CDN: `<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js">`
* 确认模态框需在 `index.html` 中定义 HTML 结构，`dashboard.js` 中管理 show/hide 逻辑
* POST 端点需在 `_setup_routes()` 中注册，使用 FastAPI 的 `@app.post` 装饰器
* 事件缓冲区放在 DashboardApp 实例上：`self._event_log: deque = deque(maxlen=200)`
* Orchestrator 状态变化时需调用 Dashboard 记录事件（通过 dashboard_app 引用或回调）
