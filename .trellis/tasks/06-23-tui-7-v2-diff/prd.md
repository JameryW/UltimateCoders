# TUI 7项界面优化V2

## Goal

在上一轮 7 项优化基础上继续提升 TUI 体验：失败诊断面板、Diff 折叠、依赖图缩进、任务摘要卡片、消息书签、输入智能补全、多行编辑增强。

## What I already know

* SubtaskItem 已有 `stderrTail` / `recentTools` 字段（linter 自动添加），但 SubtaskDetail 未展示
* ChatLog 已有 isDiffText/colorDiff 检测和渲染，但大 diff 全部展开
* SubtaskTree 已有 `buildDependedByMap` 反向依赖映射，行尾显示 `→3,4`
* TaskInput 已支持 Ctrl+J/Alt+Enter 换行，但无行号和视觉分隔
* reducer 已有 expandedIds Set 管理消息展开状态

## Requirements

### 1. 失败子任务诊断面板
* SubtaskDetail 展示 stderrTail（最后 N 行，dim 红色）
* SubtaskDetail 展示 recentTools（工具调用链：`Tools: ReadFile → WriteFile → Bash`）
* `D` 键在 ChatLog 中跳转到该失败子任务的完整输出（搜索 subtask_failed 事件消息）

### 2. Diff 折叠增强
* 大 diff（>30 行）自动折叠，显示文件头 + @@ hunk 标题 + `+N -M` 变更统计
* 折叠格式: `diff src/main.rs +15 -3 [Enter to expand]`
* 与 tool 事件折叠机制一致：Enter 展开，A 展开所有

### 3. 子任务依赖图缩进
* 根节点（无依赖）不缩进，依赖其他子任务的缩进 2 格
* 选中某个子任务时，其依赖项和被依赖项用不同颜色高亮（dep: dim yellow, dep-by: dim cyan）
* 缩进深度 = 传递依赖链长度

### 4. 任务摘要卡片
* 任务完成时自动生成摘要卡片消息，替代散落的 subtask_completed 消息
* 卡片格式: Box 边框 + 每个子任务一行（✓/✗ + 描述 + 耗时）+ 修改文件列表 + 总耗时
* 卡片插入在 task_completed 事件前

### 5. ChatLog 消息书签
* `B` 键给选中消息加书签（★ 标记在时间戳前）
* `Shift+B` 跳转到下一个书签
* 书签 ID 集合存入 reducer (bookmarkedIds: Set<string>)
* 书签跨 filter 持久（filter 切换后书签消息仍然标记）

### 6. 输入智能补全
* 文件路径补全：输入包含 `/` 或 `./` 的路径后 Tab 列出目录内容（基于 workspace 文件列表）
* 命令参数补全：`/task ` 后 Tab 显示已有 task ID 列表（从 taskList state 读取）
* 历史匹配：输入前几个字后 Tab 自动匹配最近的 inputHistory 条目

### 7. 多行编辑增强
* 多行模式下左侧显示行号（dim 灰色）
- 当前输入行高亮（左侧 `>` 标记）
* Shift+Enter 提交（当内容包含换行时）；单行模式下 Enter 提交不变

## Acceptance Criteria

* [ ] SubtaskDetail 展示 stderrTail 和 recentTools
* [ ] D 键跳转到失败子任务在 ChatLog 中的输出
* [ ] 大 diff (>30行) 自动折叠，显示文件头 + 统计
* [ ] 子任务依赖图缩进，选中时依赖项高亮
* [ ] 任务完成后显示摘要卡片
* [ ] B 键加书签，Shift+B 跳转
* [ ] 文件路径 Tab 补全
* [ ] /task 参数 Tab 补全
* [ ] 历史匹配 Tab 补全
* [ ] 多行模式显示行号
* [ ] 新快捷键注册在 keymap.ts
* [ ] TypeScript 编译 + 测试通过

## Definition of Done

* 所有 Acceptance Criteria 通过
* npx tsc --noEmit 零错误
* npx vitest run 全绿

## Out of Scope

* Web Dashboard 改动
* 外部 LLM 补全（仅本地 workspace 文件列表）
* 多行输入语法高亮

## Technical Approach

### 文件修改清单

| 文件 | 改动 |
|------|------|
| `tui/src/reducer.ts` | 增加 bookmarkedIds, tabCompletionItems, tabCompletionIndex actions |
| `tui/src/keymap.ts` | 注册 B/Shift+B (书签), D (诊断跳转) |
| `tui/src/components/ChatLog.tsx` | Diff 折叠逻辑, 书签渲染/跳转, 诊断跳转, 任务摘要卡片渲染 |
| `tui/src/components/SubtaskTree.tsx` | 依赖图缩进, 依赖高亮, SubtaskDetail 展示 stderrTail/recentTools |
| `tui/src/components/TaskInput.tsx` | 多行行号, Tab 补全, Shift+Enter 提交 |
| `tui/src/components/App.tsx` | 集成新快捷键, 诊断跳转逻辑, 任务摘要生成 |

### 依赖图缩进算法

```
function computeDepDepth(subtask, allSubtasks, memo): number
  if no dependsOn → 0
  if memo[subtask.id] → return memo[id]
  max depth of dependencies + 1
```

### 任务摘要卡片格式

```
╭─ Task Summary ──────────────────────╮
│ ✓ #1 实现API (12s)                   │
│ ✓ #2 编写测试 (8s)                    │
│ ✗ #3 集成测试 (5s) → failed          │
│ ─────────────────────────────────────│
│ Modified: src/api.rs, tests/api.rs    │
│ Total: 25s │ 2/3 done                │
╰──────────────────────────────────────╯
```
