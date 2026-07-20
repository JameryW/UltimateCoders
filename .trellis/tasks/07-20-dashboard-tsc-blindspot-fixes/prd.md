# PRD: Dashboard tsc 盲点错误修复（含 hover 崩溃）

## 背景

/loop 第 34 轮。ci-dashboard 的 `tsc --noEmit` 是空操作（project refs，见 memory dashboard-ci-tsc-noop），src 真实类型错误长期不可见。本轮用 `tsc -p tsconfig.app.json --noEmit` 把 18 个既有错暴露并修复其中可纯前端解决的 13 个；剩 5 个属 proto/后端范畴，推迟。

## 清单（已核实 + 修复）

### F77: MetricsTrendChart hover tooltip 崩溃（HIGH，真 bug）

`MetricsTrendChart.tsx:170` `activeLines.map(({ line }) => ...)` 解构出 `line`，但 `TrendLine` 接口（`{ key, label, color, transform?, format }`）无 `line` 字段 -> `line` 恒 `undefined` -> `line.key`（171）`TypeError`，hover 即崩。

修：`({ line }) =>` -> `(line) =>`（参数即 TrendLine）；`trend[hoveredIdx][line.key]` -> `trend[hoveredIdx]![line.key]`（外层 `trend[hoveredIdx] &&` 已保证非空，闭包内 TS 不传导收窄，`!` 合法）。

### F78: TasksPanel `TaskSummary` 未导入 + subtask `assignedWorker` 类型缺失

- `TasksPanel.tsx:23` `sortTasks(tasks: TaskSummary[], …)` 用 `TaskSummary` 但未 import（类型存在于 `types/dashboard.ts:76`）。修：import 补 `TaskSummary`。
- `TasksPanel.tsx:135` `s.assignedWorker` 报错——`GrpcSubmitResult.subtasks` 元素类型（useGrpcWeb.ts:91）未声明 `assignedWorker`，但 useGrpcWeb:322 实际映射了它（proto subtask 有该字段，322 无错）。修：subtasks 元素类型补 `assignedWorker?: string`。

### F79: InteractionLog `unknown && JSX` 不是 ReactNode

`InteractionLog.tsx:51-52` `{d.tool && <p>…}` / `{d.input && <pre>…}`，`d` 是 `Record<string, unknown>`，`unknown && JSX` 类型为 `unknown | JSX`，不可赋 ReactNode。修：`Boolean(d.tool) &&` / `Boolean(d.input) &&`（行为不变，已用 `String(...)` 渲染）。

### F80: MetricsPanel `thresholds[i]` possibly undefined（noUncheckedIndexedAccess）

`MetricsPanel.tsx:56-58` `pctClass(pct, thresholds = [60,85])` 用 `thresholds[0]`/`thresholds[1]`。修：签名改 `thresholds: [number, number] = [60, 85]` + 解构 `const [lo, hi] = thresholds`（tuple 解构不产生 undefined）。调用方均传 2 元数组或默认。

### F81: RepoManagementPanel EmptyState 缺 `icon` + Badge `variant="secondary"` 非法

- `:142` `<EmptyState title=… description=… />` 缺必需 `icon`。修：补 `icon="folder"`。
- `:192` `<Badge variant="secondary">`（repo tag）——Badge variant 仅状态语义（ok/degraded/error/unavailable/closed/open/half_open）。修：Badge variant 联合补 `"secondary"` + `index.css` 加 `.badge-secondary`（dark `#374151`/`#d1d5db`，light `#f3f4f6`/`#4b5563`，中性灰，tag 用）。

### F82: useGrpcWeb submitTask `{ cause }`（ES2022 lib 不支持，既有）

`useGrpcWeb.ts:327` `throw new Error(…, { cause: err })`——ErrorOptions 是 ES2022，项目 lib 是 ES2020。修：去掉 `{ cause }`（与 F74 的 unaryWithTimeout 一致；`error.cause` 在 dashboard 从不读取）。

## 验收

- `tsc -p tsconfig.app.json --noEmit`：18 -> 5（所触文件零新错，修复 13 个既有错）。
- `vite build` 通过。
- feature branch + PR + CI green。

## 不做（下轮，proto/后端）

剩 5 个错是 proto 字段缺失，需扩 proto + 后端填充 + regen，非纯前端：
- `endpoints.ts:33-34`：`RepoIndexStateProto` 无 `remoteUrl`/`defaultBranch`（仅 `IndexRepoRequest` 有）-> `RepoInfo.remote_url`/`default_branch` 恒 undefined。
- `useDashboardGrpc.ts:105-107`：`ExecutionHistoryProto` 无 `startedAt`/`completedAt`/`resultSummary`（仅有 `executedAt`/`error`/`jobName`）-> 执行历史时间戳/摘要恒 undefined。
