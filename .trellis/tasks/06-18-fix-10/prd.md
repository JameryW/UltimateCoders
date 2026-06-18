# fix: 前后端交互深度问题(第二轮)

## Goal

修复第二轮深度分析发现的 10 项前后端交互问题，涵盖数据丢失、逻辑 bug、性能和体验。

## Requirements

### P1 — 数据丢失 / 逻辑 Bug

1. **`_get_tasks_data` 不返回 subtasks 详情** (`app.py:684-718`)
   - REST API `/dashboard/api/tasks` 只返回 `subtask_count`，没有 `subtasks` 数组
   - 修复：在 `_get_tasks_data` 中加入 subtasks 详情（与 submit_task_api 返回格式一致）

2. **NATS pause/resume 返回假状态** (`app.py:418-480`)
   - NATS publish 后直接返回 success，但实际是否执行未知
   - 修复：与 submit 路径一致，加 0.3s 后检查 Orchestrator 状态确认

### P2 — 逻辑隐患 / 架构

3. **SSE 优先消费 local emitter，NATS 事件饿死** (`app.py:257-285`)
   - 循环中先检查 emitter，有事件就 continue，NATS queue 只在 emitter timeout 后检查
   - 修复：交替检查，或在每次循环中同时检查两者（先 emitter 非阻塞，再 NATS 非阻塞，都空才 snapshot）

4. **NatsWorker 同步循环阻塞消息处理** (`nats_worker.py:460-514`)
   - `_execute_subtasks` 是同步 for loop，阻塞 NATS callback
   - 修复：用 `asyncio.create_task` 在后台执行，`_handle_submit` 立即返回

5. **SSE snapshot 覆盖 gRPC 增量更新** (`useDashboard.ts:76-129`)
   - snapshot 的 `updated_at` 因生成延迟可能比 gRPC 增量更新新，覆盖更准确的状态
   - 修复：在 `handleSnapshot` 中，对 subtask 级别也做 status rank 比较（当前只做 task 级别）

6. **SSE snapshot 推全量数据，带宽浪费** (`app.py:287-299`)
   - 每 2-5 秒推送完整 snapshot（可能几十 KB）
   - 修复：增量 snapshot — 只包含自上次推送后变化的部分；或延长无事件时 snapshot 间隔到 10s

### P3 — 兼容性 / 安全 / 体验

7. **`_get_nats_event_queue` 跨 loop** (`app.py:940-957`)
   - 在无 running loop 时创建新 event loop，Queue 可能跨 loop
   - 修复：去掉 workaround，直接在 `__init__` 中创建 Queue（Python 3.10+ 不需要 workaround）

8. **CORS `*` + auth 搭配** (`app.py:161-166`)
   - `allow_origins=["*"]` 与 auth gate 不一致
   - 修复：当 DASHBOARD_PASSWORD 设置时，限制 CORS origins（从环境变量 `UC_CORS_ORIGINS` 读取）

9. **events API 无分页** (`app.py:549-570`)
   - 只有 limit，没有 offset/cursor
   - 修复：添加 `offset` 参数支持向前翻页

10. **event_log deque 和 API limit 不对齐** (`app.py:107, 560`)
    - `deque(maxlen=200)` + `events_api default limit=100`
    - 修复：增大 deque maxlen 到 500，使 API limit=100 有足够余量

## Acceptance Criteria

- [ ] REST `/dashboard/api/tasks` 返回 subtasks 详情
- [ ] NATS pause/resume 有状态确认（0.3s 后检查）
- [ ] SSE 不再饿死 NATS 事件
- [ ] NatsWorker 可并行处理多个任务
- [ ] gRPC 增量更新不被 SSE snapshot 覆盖
- [ ] SSE 无事件时 snapshot 间隔延长
- [ ] `_get_nats_event_queue` 不再跨 loop
- [ ] auth 模式下 CORS 受限
- [ ] events API 支持 offset 分页
- [ ] deque maxlen 对齐 API limit

## Out of Scope

- SSE Last-Event-ID 断线续传
- SSE token 迁移到 cookie
- 虚拟滚动库引入

## Technical Notes

### 涉及文件

| 文件 | 修改项 |
|------|--------|
| `python/ultimate_coders/dashboard/app.py` | #1 subtasks, #2 状态确认, #3 交替检查, #6 增量, #7 去workaround, #8 CORS, #9 分页, #10 deque |
| `python/ultimate_coders/nats_worker.py` | #4 create_task |
| `dashboard/src/hooks/useDashboard.ts` | #5 subtask rank |

### 关键约束

- NatsWorker `create_task` 需要确保 task 执行期间 worker 不被 stop
- SSE 交替检查不能引入额外延迟（两个源都非阻塞检查）
- CORS origins 环境变量需向后兼容（不设置时仍用 `*`）
