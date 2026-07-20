# PRD: Dashboard ErrorBoundary 覆盖收尾（根边界 + TaskDetail）

## 背景

/loop 第 37 轮。dashboard tsc 盲点已清零（PR #335-#337）。本轮转韧性：审计 ErrorBoundary 覆盖。起因是 PR #335 修的 F77（MetricsTrendChart hover 崩溃）--若无 ErrorBoundary 兜底，这类渲染崩溃会白屏整个 dashboard。

## 清单（已核实 + 修复）

### F85: TaskDetail 是唯一未包 ErrorBoundary 的面板（MED，韧性）

`App.tsx` 所有面板（Tasks/Event Log/Task Activity/Metrics/Code Search/Files/Repos/Workers/Health/Scheduler）都包了 `<ErrorBoundary>`，唯独右栏 `TaskDetail`（App.tsx:534）没包。TaskDetail 访问 `task.id`/`task.subtasks`/`task.description` 等，任务数据边缘形态可致渲染崩溃。无边界兜底 -> 崩溃上抛，无最近边界 -> 白屏（见 F86）。

修：`<ErrorBoundary name="Task Detail">` 包 TaskDetail，与其他面板一致。

### F86: 无根 ErrorBoundary，未兜底崩溃白屏（MED，韧性）

`main.tsx` 的 `createRoot` 直接 `<Root />`（内含 `<App/>`/`<TuiPage/>`），无根边界。面板级边界兜各面板，但**逃逸**的崩溃（未包的组件、hook 错误、路由层）无兜底 -> 整个 dashboard 白屏，用户只能手动 reload。

修：
- `ErrorBoundary` 加 `fallbackRender?: (error, retry) => ReactNode`（向后兼容，既有 `fallback`/默认仍工作），让 fallback 能显示错误信息 + 自定义恢复动作。
- `main.tsx` 加根 `<ErrorBoundary fallbackRender={RootErrorFallback}>` 包 `<Root/>`：显示错误信息 + Retry（重渲染，清瞬态）+ Reload（整页重载，清确定性崩溃）。

## 验收

- `tsc -p tsconfig.app.json --noEmit`：0（零新错，盲点已清零状态保持）。
- `vite build` 通过。
- feature branch + PR + CI green（ci-dashboard）。

## 不做

无。后续可转 Rust gRPC / Python worker 审计（CLAUDE.md 架构面未审计，见 07-20-data-layer-low-sweep PRD）或停 /loop（需用户决策）。
