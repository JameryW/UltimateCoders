# PRD: Dashboard 加载链路 CRITICAL/HIGH 修复（新审计 #1/#2/#3）

## 背景

/loop 第 29 轮。dashboard 前端重审 15 finding，取 CRITICAL + 2 HIGH（全 S）：

## 清单（已核实）

### F67: Tasks panel 启动永不加载（审计 #1，CRITICAL）

三缺陷叠加 → 健康启动下初始任务列表**永不拉取**：
1. `skipTasks = grpcState === "connected"`（App.tsx:187）——两个 stream hook 挂载时同步置 "connected"（乐观，审计 #11），auth 解析完时 skipTasks 恒真；
2. `fetchTasks` 仅在 `grpcState === "connected"` 时传（App.tsx:193）——恰是 skipTasks 使其无用之时，两分支互斥 → `fetchInitial` **永不调 listTasks**；
3. 回退 merge effect（App.tsx:202）要求 `dashGrpcState !== "connected" && grpcState === "connected"`——启动时两者同批翻转，转移永不发生；WatchTask taskId:"" 跳过 replay（server.rs:3264）流也不回填。

净效果：`tasks.available` 恒 false → TasksPanel 渲染 "Unavailable" 徽章且**整个列表隐藏**（TasksPanel.tsx:196 三目），即便 live 事件在更新 state。另 `mergeGrpcTasks`（useDashboard.ts:339）spread prev 从不置 `available: true` → 同步触发的 merge 也救不回徽章。

修：App.tsx 恒传 `fetchTasks: listTasks` + 删 `skipTasks`（启动拉一次）；`mergeGrpcTasks` 置 `available: true`（data.available 已过 guard）。

### F68: #/tui 每次加载必崩（审计 #2，HIGH）

TuiPage.tsx:21-34：`if (auth.isChecking) return (...)` 在 `useEffect`（L32）**之前**。首渲染跑 3 个 hook；isChecking 翻 false 后第 4 个 hook 出现 → React 抛 "Rendered more hooks than during the previous render"，页面卸载。useAuth 起始 isChecking 恒 true → **每次导航 #/tui 必崩**（PR #296 修 render-setState 时把 effect 移到 gate 之下引入的回归）。

修：redirect useEffect 移到早返回之上（hook 顺序恒定）。

### F69: 鉴权端点裸调（审计 #3，HIGH）

后端所有 `/dashboard/api/*` 非 localhost 走 DASHBOARD_PASSWORD 门（Bearer 或 ?token=）。三调用方两者皆无：
- `new EventSource("/dashboard/api/stream")`（useDashboardGrpc.ts:342）——EventSource 不能设 header → SSE fallback 在远程部署**永远 401 重连循环**；
- `fetch('/dashboard/api/trend?...')`（MetricsPanel.tsx:294）——6h/24h trend 永久不可用；
- `fetch("/dashboard/api/alerts?limit=100")`（alert-bar.tsx:84）——告警历史永久不可用。

localhost 绕过鉴权 → 仅恰是开了鉴权的部署静默坏。

修：useAuth 导出 STORAGE_KEY；三处 URL 追加 `token=<encodeURIComponent(token)>`（stream 用 `?`，已有 query 的用 `&`）。

## 验收

- dashboard 构建通过：`cd dashboard && npx tsc -p tsconfig.app.json --noEmit`（或 vite build）——src/ 类型干净（memory：CI tsc 是 no-op，须本地验）。
- 手动推理三修复路径（React hook 规则、URL 拼接）。
- feature branch + PR + CI green（ci-dashboard）。

## 不做（后续轮）

#4 快照重放事件重复/toast（M，dedup 集）；#5 SSE snapshot 丢 alert 字段；#6 trend 每快照重拉；#7 connectionError 不可达；#8-#15 杂项。
