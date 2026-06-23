# TUI 7项界面优化

## Goal

一次性完成 7 项 TUI 体验升级，大幅提升信息密度和操作效率：消息自动折叠、子任务耗时、Worker并行面板、消息按子任务分组、命令面板、宽屏双栏布局、快捷键发现性。

## What I already know

* ChatLog 已有虚拟滚动 (estimateMsgHeight + binary search) + tool 事件折叠 (COLLAPSE_THRESHOLD=3, extractToolSummary)
* SubtaskTree 已有状态色块 + 依赖箭头 + keyboard nav + detail panel
* StatusBar 已有 segment-based responsive layout + worker summary
* Reducer 已有 focusedArea (input|chat), eventFilter, search, overlay state
* Keymap 已集中管理 (keymap.ts)，状态条从 getStatusBarHelp 读 hint
* 已有命令自动补全 (commandSuggestions in reducer)，但只支持 `/` 前缀

## Assumptions (temporary)

* 子任务耗时数据需要从 useTaskEvents 的事件时间戳计算
* Worker 活动流数据可从 subtasks 的 assignedWorker + status 派生
* 宽屏双栏不需要修改 Ink 的布局引擎，用 Box flex 即可实现
* 命令面板可复用现有的 CjkTextInput 组件

## Open Questions

(none — all decisions resolved below)

## Requirements

### 1. 消息自动折叠增强
* tool_result 超 20 行自动折叠，显示前 3 行 + `... [+N lines — Enter to expand]`
* diff 文件自动折叠：大 diff 只显示文件头 (diff --git...) + 变更统计
* `A` 键展开所有折叠消息 (chat focus); `Shift+A` 折叠所有
* 折叠行数阈值可通过 COLLAPSE_THRESHOLD 常量调节 (当前 3)

### 2. 子任务耗时显示
* SubtaskItem 增加 `elapsedMs?: number` 字段
* useTaskEvents 中从事件时间戳计算: subtask_started → subtask_completed/failed 的间隔
* SubtaskTree 行格式: `✓ #1 实现API (12s)` — 状态图标后追加耗时
* 失败重试场景: 显示最近一次耗时
* pending/assigned 状态不显示耗时

### 3. Worker 并行状态面板
* 新组件 `WorkerPanel.tsx`，放在 StatusBar 上方 (workersExpanded 时展开)
* 每行: `Worker-1 [████░░] 2/3 done  running: #3 "写测试"`
* 数据源: 从 subtasks 的 assignedWorker + status 派生
* 默认折叠 (仅 StatusBar 显示 3/5 active)，Ctrl+Shift+W 展开

### 4. 消息按子任务分组
* ChatLog 增加子任务分隔线: `── Subtask #1: 实现API ──────────`
* 分隔线在 subtask_started 事件消息前显示
* 非子任务事件 (task_submitted 等) 不加分隔线
* 分隔线用 dim 白色/灰色，不占多余行

### 5. 命令面板
* Ctrl+P 打开命令面板 overlay
* 可搜索所有 slash 命令: /tasks, /clear, /health, /search 等
* 复用 CjkTextInput 做搜索输入
* Enter 执行选中命令，Esc 关闭
* 与现有 commandSuggestions 逻辑合并

### 6. 宽屏双栏布局
* terminal width >= 120 时: 左栏 ChatLog，右栏 SubtaskTree + WorkerPanel
* 80-119: 当前单栏布局 (ChatLog 上方，SubtaskBar 下方)
* <80: 紧凑模式 (SubtaskBar 缩为一行)
* 右栏宽度固定 40 列，左栏占剩余空间
* 用 getLayoutMode() 已有的 'wide' 分支实现

### 7. 快捷键发现性
* 首次启动时显示 3 秒欢迎 Banner: `按 ? 查看快捷键 | Shift+Tab 切换焦点`
* StatusBar 空闲 5 秒后轮播快捷键 hint (每 5 秒切换一个)
* LogoBanner 组件改造为欢迎 Banner，3 秒后自动消失

## Acceptance Criteria

* [ ] tool_result 超 20 行自动折叠，Enter 展开
* [ ] `A` 展开所有 / `Shift+A` 折叠所有
* [ ] 子任务完成/失败后显示耗时 (如 `✓ 12s`)
* [ ] workersExpanded 时 StatusBar 上方显示 WorkerPanel
* [ ] WorkerPanel 每行显示 Worker ID + 负载条 + 当前子任务
* [ ] ChatLog 在 subtask_started 消息前显示分组分隔线
* [ ] Ctrl+P 打开命令面板 overlay，可搜索执行 slash 命令
* [ ] 宽屏 (≥120 cols) 双栏布局生效
* [ ] 首次启动显示欢迎 Banner，3 秒后消失
* [ ] StatusBar 空闲轮播快捷键 hint
* [ ] 所有新快捷键注册在 keymap.ts
* [ ] npm run check 通过 (typecheck + lint)

## Definition of Done

* 所有 Acceptance Criteria 通过
* npm run check (typecheck + lint) 绿色
* 手动终端验证各功能

## Out of Scope

* Web Dashboard 改动 (本任务仅限 TUI)
* 多 Worker 远程 gRPC 连接 (仅用本地 subtasks 数据)
* 命令面板支持自定义命令注册
* 子任务耗时精确到毫秒级 (秒级足够)

## Technical Approach

### 文件修改清单

| 文件 | 改动 |
|------|------|
| `tui/src/reducer.ts` | 增加 expandAll/collapseAll actions, commandPaletteOpen, welcomeBannerVisible, hintRotationIndex |
| `tui/src/keymap.ts` | 注册 A/Shift+A, Ctrl+P 新命令 |
| `tui/src/components/ChatLog.tsx` | 折叠阈值调整 (20→3 header lines), 分组分隔线, expandAll/collapseAll 逻辑 |
| `tui/src/components/SubtaskTree.tsx` | elapsedMs 显示 |
| `tui/src/components/WorkerPanel.tsx` | 新建: Worker 活动面板 |
| `tui/src/components/CommandPalette.tsx` | 新建: 命令面板 overlay |
| `tui/src/components/App.tsx` | 宽屏双栏布局, WorkerPanel 集成, 命令面板 overlay, 欢迎 Banner 逻辑, hint 轮播 |
| `tui/src/components/StatusBar.tsx` | hint 轮播逻辑 |
| `tui/src/components/LogoBanner.tsx` | 改造为欢迎 Banner (3 秒自动消失) |
| `tui/src/hooks/useTaskEvents.ts` | 计算 elapsedMs 并写入 SubtaskItem |
| `tui/src/components/SubtaskTree.tsx` | SubtaskItem 类型增加 elapsedMs |

### 子任务耗时计算逻辑

在 `useTaskEvents.ts` 的 `processEvent` 中:
- `subtask_started`: 记录 `startedAtMs = new Date(event.timestamp).getTime()` 到 SubtaskItem
- `subtask_completed` / `subtask_failed`: 计算 `elapsedMs = now - startedAtMs`

### Worker 面板数据派生

从 subtasks 数组派生:
```ts
function deriveWorkerSummary(subtasks: SubtaskItem[]): WorkerSummary {
  const workerMap = new Map<string, {active: SubtaskItem[], completed: number, failed: number}>();
  for (const st of subtasks) {
    if (!st.assignedWorker) continue;
    const entry = workerMap.get(st.assignedWorker) ?? {active: [], completed: 0, failed: 0};
    if (st.status === 'in_progress') entry.active.push(st);
    if (st.status === 'completed') entry.completed++;
    if (st.status === 'failed') entry.failed++;
    workerMap.set(st.assignedWorker, entry);
  }
  // ... build summary
}
```

### 双栏布局实现

App.tsx 中 `wide` 模式:
```tsx
{layoutMode === 'wide' ? (
  <Box flexDirection="row">
    <Box flexGrow={1}><ChatLog ... /></Box>
    <Box flexDirection="column" width={40}>
      <SubtaskTree ... />
      {workersExpanded && <WorkerPanel ... />}
    </Box>
  </Box>
) : ( /* 当前单栏布局 */ )}
```

## Decision (ADR-lite)

**Context**: 7 项优化需在一个 PR 内完成，需要最小改动路径
**Decision**: 复用现有组件和 reducer 模式，不改架构；Worker 数据从 subtasks 派生而非新增 gRPC 调用
**Consequences**: Worker 面板数据可能比 gRPC 实时流慢几秒；但避免了新协议依赖，MVP 足够

## Technical Notes

* Ink 5 用 Box flex 实现 layout，无需新依赖
* CjkTextInput 已支持 IME，命令面板直接复用
* 虚拟滚动已有 estimateMsgHeight，折叠增强只需调整 COLLAPSE_THRESHOLD 和渲染逻辑
* StatusBar hint 轮播用 useState + useEffect(setInterval) 即可
