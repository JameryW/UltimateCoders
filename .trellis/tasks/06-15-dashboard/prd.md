# 制作网页 Dashboard v2

## Goal

将 v1 Dashboard（FastAPI + Jinja2 + 原生JS 嵌入式）重构为独立部署的 React + Vite SPA，后端仅提供 JSON API。提升可维护性、可扩展性，并为未来功能（认证、图表趋势）预留架构。

## Requirements

### 核心功能（复现 v1 全部功能）

- Engine Health 面板：状态 + 组件列表 + 版本/运行时间
- Workers 面板：列表 + 负载条 + 心跳状态
- Tasks 面板：状态统计 + 列表 + 详情展开（子任务 + 交互日志 + 输出文件 + Mermaid DAG）
- Scheduler 面板：运行状态 + 夜间窗口 + 定时任务 + 执行历史
- Circuit Breaker / Rate Limiter 面板：状态 + 指标 + 重置操作
- Event Log 面板：实时事件流 + 类型过滤
- Task Submit 表单：description + project_id + 提交
- 交互操作：暂停/恢复任务、重置 CB、触发调度、Flush pending
- SSE 实时更新：task_event（即时） + update（5s 全量快照）
- Mermaid DAG 渲染（子任务依赖图）
- 连接状态指示器
- 确认弹窗 + Toast 通知

### 架构需求

- 独立 SPA 项目：`dashboard/` 根目录，独立 `package.json` + `vite.config.ts`
- React 18 + Vite + TypeScript
- shadcn/ui + Tailwind CSS（暗色主题）
- Recharts 图表（MVP 先占位）
- SSE hook 封装（`useSSE` custom hook）
- API client 层（`/dashboard/api/*` 封装）
- 认证 hook 预留（`useAuth` 占位，当前 no-op）

### 开发体验

- Vite dev server (5173) → 代理 `/dashboard/api/*` 到后端 (8080)
- 后端 CORS 已配置（`allow_origins=["*"]`，无需改动）
- 后端清理：移除 Jinja2 模板渲染和 static 文件挂载（仅保留 JSON API + SSE）

### 未来预留

- `useAuth` hook 占位 → 未来接入 JWT/OAuth
- Recharts 图表占位 → 任务完成趋势、延迟分布等
- API client 层结构化 → 未来可加 WebSocket 补充 SSE

## Acceptance Criteria

- [ ] 所有 v1 功能在新 React SPA 中可用
- [ ] SSE 实时更新正常（task_event + update）
- [ ] 交互操作（暂停/恢复/提交/重置/触发/Flush）正常
- [ ] Mermaid DAG 正常渲染
- [ ] 暗色主题视觉与 v1 一致
- [ ] `pnpm dev` 启动开发服务器，Vite proxy 转发 API
- [ ] `pnpm build` 产出可部署静态文件
- [ ] 后端移除 Jinja2/static 挂载，仅保留 JSON API
- [ ] useAuth hook 占位存在（no-op）
- [ ] Recharts 组件占位存在

## Definition of Done

- TypeScript 严格模式，无 any 滥用
- ESLint + Prettier 配置
- `pnpm build` 零错误
- 后端 API 测试继续通过
- README 更新（开发/部署说明）

## Technical Approach

### 前端架构

```
dashboard/
├── package.json
├── vite.config.ts          # proxy /dashboard/api → localhost:8080
├── tsconfig.json
├── tailwind.config.ts
├── components.json         # shadcn/ui config
├── src/
│   ├── main.tsx
│   ├── App.tsx             # Layout + Router (if needed)
│   ├── hooks/
│   │   ├── useSSE.ts       # SSE connection management
│   │   ├── useAuth.ts      # Auth placeholder (no-op)
│   │   └── useDashboard.ts # Aggregated dashboard state
│   ├── api/
│   │   ├── client.ts       # Fetch wrapper + error handling
│   │   └── endpoints.ts    # Typed API functions
│   ├── components/
│   │   ├── ui/             # shadcn/ui components
│   │   ├── layout/
│   │   │   ├── Header.tsx
│   │   │   ├── ConnectionIndicator.tsx
│   │   │   └── Toast.tsx
│   │   ├── panels/
│   │   │   ├── HealthPanel.tsx
│   │   │   ├── WorkersPanel.tsx
│   │   │   ├── TasksPanel.tsx
│   │   │   ├── TaskDetail.tsx
│   │   │   ├── InteractionLog.tsx
│   │   │   ├── SchedulerPanel.tsx
│   │   │   ├── CircuitBreakerPanel.tsx
│   │   │   └── EventLogPanel.tsx
│   │   ├── charts/
│   │   │   └── TaskTrendChart.tsx  # Recharts placeholder
│   │   └── forms/
│   │       └── TaskSubmitForm.tsx
│   ├── lib/
│   │   ├── utils.ts        # shadcn cn() helper
│   │   └── mermaid.ts      # Mermaid render helper
│   └── types/
│       └── dashboard.ts    # API response types
└── public/
```

### 后端改动

- `app.py`: 移除 Jinja2 `Templates` 导入 + `templates` 目录挂载 + `StaticFiles` 挂载
- `app.py`: 移除 `GET /dashboard/` HTML 页面路由
- 保留所有 `GET /dashboard/api/*` 和 `POST /dashboard/api/*` 路由
- 保留 SSE `/dashboard/api/stream`

### 数据流

```
React SPA (5173) ──Vite proxy──> FastAPI (8080)
  useSSE hook ──EventSource──> /dashboard/api/stream
  API client  ──fetch──> /dashboard/api/*
```

## Decision (ADR-lite)

**Context**: v1 Dashboard 用 Jinja2 + 原生JS 嵌入后端，634行单文件不可维护，无法独立部署
**Decision**: React + Vite + shadcn/ui + Recharts 独立 SPA，后端仅保留 JSON API
**Consequences**:
  - (+) 前后端独立部署、独立迭代
  - (+) React 组件化、TypeScript 类型安全、shadcn/ui 暗色主题开箱即用
  - (+) Vite HMR 极速开发体验
  - (-) 增加了构建步骤和 node 依赖
  - (-) 部署需要静态文件服务器（Nginx / CDN）
  - (-) 开发需要同时启动后端 + 前端

## Out of Scope

- 认证/登录功能实现（仅预留 hook）
- 图表数据聚合后端 API（仅前端占位）
- 移动端响应式优化（保持 v1 水平即可）
- 国际化 i18n
- E2E 测试（Playwright）
- 数据持久化（v1 也没有）

## Technical Notes

- v1 代码位置:
  - 后端: `python/ultimate_coders/dashboard/app.py`
  - 前端: `python/ultimate_coders/dashboard/templates/index.html` + `static/dashboard.js`
  - Spec: `.trellis/spec/backend/dashboard-spec.md`
- API 端点完整列表见 dashboard-spec.md（10 GET + 6 POST + 1 SSE）
- SSE 两种事件类型: `task_event`（实时）+ `update`（5s 快照）
- v1 后端 CORS 已配置 `allow_origins=["*"]`
- 包管理器: pnpm（推荐，磁盘效率高）
