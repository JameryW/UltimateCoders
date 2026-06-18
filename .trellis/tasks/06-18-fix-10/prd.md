# fix: 前后端交互10项逻辑与体验问题

## Goal

修复 Dashboard 前后端交互中发现的 10 项逻辑 bug 和体验问题，提升系统可靠性和用户感知。

## What I already know

* 10 项问题已在代码分析中定位，涉及文件：`useAuth.ts`, `useSSE.ts`, `useDashboard.ts`, `useGrpcWeb.ts`, `App.tsx`, `InteractionLog.tsx`, `app.py`
* 前端双通道架构（SSE + gRPC-Web）是去重问题的根源
* 后端 DashboardApp 是 FastAPI 应用，auth 通过 DASHBOARD_PASSWORD 环境变量控制

## Requirements

### P1 — 逻辑 Bug

1. **flush_pending_api 没实际执行 flush** (`app.py:492-507`)
   - 当前只读取 count + 记录事件，不调用 `orch.flush_pending_tasks()`
   - 修复：调用 `asyncio.create_task(orch.flush_pending_tasks())` 或 await

2. **非 401 错误码默认认证通过** (`useAuth.ts:63-65`)
   - 503 等错误时 `setIsAuthenticated(true)` 是错误的
   - 修复：503/502/500 等服务端错误应视为"服务不可用"，显示连接错误页而非放行

### P2 — 逻辑隐患

3. **NATS vs legacy 返回结构不一致** (`app.py:330-348`)
   - NATS 路径返回 `status: "Planning", subtask_count: 0, subtasks: []`
   - Legacy 路径返回真实 status 和 subtask 数据
   - 修复：统一返回格式，NATS 路径也返回 `status: "submitted"` 并在注释中说明后续由 SSE 推送更新

4. **双通道去重 2s 窗口可能误去重** (`App.tsx:96-120`)
   - `eventKey()` 用 data 前 40 字符，同 subtask 快速状态变化可能碰撞
   - 修复：在 eventKey 中加入 `ev.type`（已有）并确保 data hash 足够区分；将窗口缩短到 1s 或改用递增序号

5. **SSE 断线状态闪烁** (`useSSE.ts:101-118`)
   - EventSource CONNECTING 状态时 `setConnected(false)` 导致 UI 闪烁
   - 修复：不改 debounce，改 ConnectionIndicator UI — 用 CSS transition (0.5s fade) 平滑过渡，视觉上不闪烁

### P2 — 体验

6. **三段加载等待** (`App.tsx:302-346`)
   - auth check → loading → 数据到达，用户等 3-5s
   - 修复：合并 auth check + fetchInitial 为并行；auth 成功后立即开始 fetch

7. **gRPC 断开时无 fallback 提交** (`App.tsx:383`)
   - TaskSubmitForm 在 gRPC 断开时无 REST fallback
   - 修复：TaskSubmitForm 增加 REST submit fallback

### P3 — 安全/体验/性能

8. **SSE token URL 明文暴露** (`useSSE.ts:25-29`)
   - 当前 `?token=` 方式有安全隐患
   - 修复：短期 — 改用短时 token + 限制日志记录；长期 — 迁移到 cookie（需后端配合，本 PR 不做）

9. **部分端点失败无明确提示** (`App.tsx:349`)
   - `allFailed` 阈值硬编码，部分失败时显示半残 dashboard
   - 修复：在 Header 或顶部显示"部分服务不可用"横幅

10. **事件日志无虚拟滚动** (`InteractionLog.tsx:43-59`)
    - 直接 `.map()` 渲染，长时间运行后 DOM 节点过多
    - 修复：限制渲染条数（只渲染可视区域附近），或用 CSS `content-visibility: auto`

## Acceptance Criteria

- [ ] flush_pending 按钮点击后实际执行 flush 操作
- [ ] 后端 503/502/500 时前端显示连接错误页，不误放行
- [ ] NATS 和 legacy 路径返回一致的响应结构
- [ ] 双通道去重不再误丢快速状态变化事件
- [ ] SSE 短暂断线（<3s）不触发 UI 闪烁
- [ ] 首屏加载时间减少（auth + fetch 并行）
- [ ] gRPC 断开时可通过 REST 提交任务
- [ ] 部分端点失败时显示警告横幅
- [ ] 事件日志长时间运行不卡顿

## Definition of Done

* 修改的文件 lint / typecheck 通过
* 手动验证关键路径（flush、auth 503、SSE 断线恢复）
* CI green

## Out of Scope

* SSE token 迁移到 HttpOnly cookie（需后端新增 endpoint，单独 PR）
* SSE Last-Event-ID 断线续传（需后端支持，单独 PR）
* 虚拟滚动库引入（用 CSS content-visibility 替代）

## Technical Notes

### 涉及文件

| 文件 | 修改项 |
|------|--------|
| `python/ultimate_coders/dashboard/app.py` | #1 flush, #3 NATS 返回格式 |
| `dashboard/src/hooks/useAuth.ts` | #2 503 处理 |
| `dashboard/src/hooks/useSSE.ts` | #5 debounce, #8 token 注释 |
| `dashboard/src/App.tsx` | #4 去重, #6 并行加载, #9 横幅 |
| `dashboard/src/components/forms/TaskSubmitForm.tsx` | #7 REST fallback |
| `dashboard/src/components/panels/InteractionLog.tsx` | #10 虚拟滚动 |
| `dashboard/src/components/layout/Header.tsx` | #9 横幅位置 |

### 关键约束

* 后端 `flush_pending_tasks` 是 async 方法，在 sync route handler 中需要 `asyncio.create_task` 或改 route 为 async
* SSE debounce 不能影响真正的断线检测（心跳 15s 超时）
* 去重逻辑修改需确保不引入新的重复处理
