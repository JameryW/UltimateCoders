# Dashboard 监控集群运行情况

## Goal

为 UltimateCoders 增加嵌入 Orchestrator 的 Web Dashboard，实时监控集群运行状态：引擎健康、Worker 负载、任务执行、调度器状态、夜间窗口、Circuit Breaker、Rate Limiter。让运维和开发者一眼看清系统全貌。

## Requirements

### 核心功能

1. **引擎健康面板**
   - 显示 LocalEngine health() 返回的 11 个组件状态（ok/degraded/error）
   - 整体状态汇总（ok → 绿, degraded → 黄, error → 红）
   - 组件详情：TiKV、Qdrant、PostgreSQL 连接状态；Index Pipeline、Search Engine 可用性；Embedding Service 模型/维度；Circuit Breaker 状态/失败数；Rate Limiter RPM/TPM 额度

2. **Worker 列表面板**
   - 已注册 Worker 列表：id, capabilities, current_load/max_capacity, last_heartbeat
   - 负载进度条可视化
   - 心跳超时标记（> heartbeat_timeout_seconds 显示 warning）

3. **任务状态面板**
   - Active tasks 列表：id, description, status, project_id
   - 状态统计：CREATED / PLANNING / IN_PROGRESS / COMPLETED / FAILED / PAUSED 计数
   - 夜间窗口排队任务数 (pending_task_count)

4. **调度器面板**
   - 调度器运行状态 (is_running)
   - 夜间窗口配置及当前状态 (check_night_window)
   - 已注册调度任务列表：description, cron/execute_after, project_id, enabled
   - 最近执行历史：task_id, started_at, completed_at, status, result_summary

5. **Circuit Breaker + Rate Limiter 面板**
   - Circuit Breaker：state (Closed/Open/HalfOpen), failure_count, total_calls, total_rejected
   - Rate Limiter：rpm_available, tpm_available

6. **SSE 实时推送**
   - FastAPI SSE endpoint：每 5 秒推送状态快照
   - 前端 EventSource 监听，自动更新面板
   - 连接断开自动重连

7. **Fallback 模式**
   - 无 Orchestrator/Scheduler 时，对应面板显示 "Not Available"
   - 无基础设施（TiKV/Qdrant/PG）时，引擎健康面板显示 degraded/error 但不崩溃

### 技术架构

```
Browser ──SSE──> FastAPI (/dashboard/api/stream)
              ──GET──> FastAPI (/dashboard/api/*)
                          │
                     Orchestrator (嵌入)
                          │
                ┌─────────┼──────────┐
                │         │          │
          Engine.health()  workers   Scheduler
          (PyO3/Rust)     tasks     jobs/history
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/dashboard/` | GET | Dashboard HTML 页面 |
| `/dashboard/api/health` | GET | 引擎健康 JSON |
| `/dashboard/api/workers` | GET | Worker 列表 JSON |
| `/dashboard/api/tasks` | GET | 任务状态 JSON |
| `/dashboard/api/scheduler` | GET | 调度器状态 JSON |
| `/dashboard/api/stream` | GET | SSE 事件流 |

### 启动方式

```python
orch = Orchestrator(scheduler=scheduler, engine=engine)
orch.start_dashboard(host="0.0.0.0", port=8080)
```

### UI 设计

- Jinja2 模板 + Tailwind CSS CDN（无需 node/npm）
- 深色主题，适合运维监控
- 卡片式布局：4 列网格（健康/Worker/任务/调度）
- SSE 驱动，无全页刷新
- 移动端自适应（单列堆叠）

## Acceptance Criteria

* [ ] `orch.start_dashboard()` 启动 FastAPI 服务，浏览器可访问
* [ ] 引擎健康面板显示 11 个组件状态 + 整体汇总
* [ ] Worker 面板显示注册 Worker 列表和负载
* [ ] 任务面板显示 active tasks 和状态统计
* [ ] 调度器面板显示 jobs、night window、执行历史
* [ ] Circuit Breaker + Rate Limiter 状态可见
* [ ] SSE 推送每 5s 自动更新面板
* [ ] 无基础设施时 fallback 显示（不崩溃）
* [ ] Python 单元测试通过
* [ ] `cargo clippy` + `cargo test` + `pytest` green

## Definition of Done

* Tests added/updated (Python unit + integration)
* `pytest` green
* Docs/architecture.md 更新（Dashboard 架构章节）
* Docker compose 集成（dashboard port 映射）

## Technical Approach

### 架构分层

```
python/ultimate_coders/
├── dashboard/
│   ├── __init__.py
│   ├── app.py              # FastAPI app + routes + SSE
│   ├── templates/
│   │   └── index.html      # Jinja2 + Tailwind 主页面
│   └── static/
│       └── dashboard.js    # SSE 客户端 + DOM 更新
├── agent/
│   ├── orchestrator.py     # 增加 start_dashboard() / stop_dashboard()
│   ├── scheduler.py        # 已有，Dashboard 直接调用
│   └── types.py            # 已有，WorkerInfo/Task/TaskStatus
```

### 关键实现

1. **`DashboardApp`** (`dashboard/app.py`)
   - FastAPI 实例，接收 Orchestrator 引用
   - REST API endpoints 返回 JSON
   - SSE endpoint：`async def stream()` → `EventSourceResponse`
   - Jinja2 模板渲染

2. **`Orchestrator.start_dashboard()`**
   - 创建 `DashboardApp(self)` 
   - `uvicorn.run()` 在后台线程启动
   - `stop_dashboard()` 优雅关闭

3. **前端 `dashboard.js`**
   - `new EventSource('/dashboard/api/stream')`
   - 解析 JSON 事件 → 更新 DOM
   - 断连自动重连（EventSource 内置）

### 依赖变更

- 新增: `fastapi`, `uvicorn`, `jinja2`, `sse-starlette`

## Decision (ADR-lite)

**Context**: 需要为 UltimateCoders 增加 Web Dashboard 监控集群运行情况。需选择技术栈和部署方式。
**Decision**: 方案 A — FastAPI + Jinja2 + Tailwind + SSE，嵌入 Orchestrator
- Python FastAPI 提供 REST API + SSE 推送 + Jinja2 模板渲染
- 嵌入 Orchestrator 进程，直接读取内存状态（workers/tasks/scheduler），零延迟
- SSE 推送代替 WebSocket，实现简单、浏览器原生支持
- Tailwind CSS CDN，无需 node/npm build
**Consequences**:
- 需新增 Python 依赖 (fastapi, uvicorn, jinja2, sse-starlette)
- Dashboard 进程与 Orchestrator 共存，崩溃可能相互影响
- 后续如需独立部署，拆分成本低（API 层已就绪）

## Out of Scope

* 任务操作控制（创建/取消/重试任务）
* 告警通知（email/slack/pagerduty）
* 历史趋势图表（Prometheus + Grafana）
* 多集群监控
* 用户认证/权限
* WebSocket

## Implementation Plan (small PRs)

* **PR1**: Dashboard 骨架 + FastAPI app + Jinja2 模板 + health 面板
* **PR2**: Worker + Task + Scheduler 面板 + SSE 推送
* **PR3**: Circuit Breaker / Rate Limiter 面板 + Docker 集成 + 文档

## Technical Notes

* `crates/uc-engine/src/local.rs:483-582` — LocalEngine health() 实现 (11 组件)
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator 状态 (workers, tasks, pending)
* `python/ultimate_coders/agent/scheduler.py` — Scheduler API
* `crates/uc-engine/src/scheduler/service.rs` — SchedulerService
* `crates/uc-engine/src/circuit_breaker.rs` — CircuitBreaker metrics
* `crates/uc-engine/src/rate_limiter.rs` — RateLimiter metrics
* `crates/uc-python/src/engine.rs` — PyEngine health()
* `python/ultimate_coders/engine.py` — Python Engine.health()
* `docker-compose.yml` — 基础设施 health checks
