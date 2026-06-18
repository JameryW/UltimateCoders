# PRD: 前后端交互8项逻辑与体验问题修复

## 问题清单

### P0
1. **_get_tasks_data 不返回 subtasks** — 后端 tasks API 只返回 subtask_count，不返回 subtask 列表。SSE snapshot 也不含 subtask 详情，前端刷新后丢失。
2. **flush_pending 假操作** — API 只返回 count，没有实际触发 flush。

### P1
3. **NATS pause/resume 假确认** — publish 后直接返回 success，不知道后端是否真的执行了。
4. **6 个串行 API 调用** — fetchInitial 串行请求 6 个端点，任何一个慢都阻塞渲染。
5. **503/5xx 当认证成功** — useAuth 把非 200 非 401 的状态码当 isAuthenticated:true。

### P2
6. **NATS submit 返回假数据** — subtask_count:0, subtasks:[] 是编造的，前端先闪再更新。
7. **断连后放弃重试** — SSE 和 gRPC-Web 最多重试 5 次就放弃，用户需手动重连。
8. **fetchInitial 和 SSE 竞争** — SSE snapshot 可能覆盖 fetchInitial 的新数据。

## 修复方案

### #1 _get_tasks_data 加 subtasks
- `app.py:_get_tasks_data()` 在 task dict 中加 `subtasks` 字段，遍历 `t.subtasks` 输出详情

### #2 flush_pending 实际执行
- `app.py:flush_pending_api` 调用 `orch.flush_pending_tasks()` (async)，用 `asyncio.create_task` 触发

### #3 NATS pause/resume 加确认
- 改为 request-response 模式：publish 后等待 nats_worker 回复确认，超时则 fallback
- 简化方案：publish 后做一次 get_task 状态检查确认变更

### #4 fetchInitial 并行化
- `useDashboard.ts:fetchInitial` 改用 `Promise.all` 并行请求

### #5 useAuth 修复 5xx 处理
- 502/503/504 等服务端错误设 `connectionError: true`，不当作认证成功

### #6 NATS submit 返回真实数据
- 改为 request-response：publish 后等待 nats_worker 返回 task 结果
- 简化方案：publish 后立即用 task_id 调 get_task 获取真实状态

### #7 无限重试 + 用户提示
- SSE: 去掉 MAX_RETRY 上限，改用指数退避无限重试（cap 60s）
- gRPC-Web: 同理，exhausted 状态改为持续重试
- UI: ConnectionIndicator 在重试中显示 "Reconnecting..."

### #8 fetchInitial 用 merge 而非 replace
- fetchInitial 的 tasks 结果通过 mergeGrpcTasks 合入，而非直接 setTasks
- handleSnapshot 已有 merge 逻辑，fetchInitial 也应走同一路径
