# Dashboard UI 完善 + 页面布局和视觉效果优化

## Goal

完善 UltimateCoders Dashboard 的功能完整性和视觉体验，使其从一个功能可用但视觉粗糙的监控面板，升级为专业、美观、信息密度合理的运维 Dashboard。

## Requirements

### 布局重构

* 双栏主次布局 — 左侧大栏 (8/12) 放核心面板 (Tasks+EventLog+Chart+Search+Files)，右侧窄栏 (4/12) 放辅助面板 (Workers+Health+CB+Scheduler)
* 类似 Grafana 侧边栏模式，移动端回退到单列

### 面板交互

* 右侧栏辅助面板默认折叠只显示标题+关键指标（如 Workers "3/5 online"），点击展开看详情
* 点击 Task 时右侧栏切换为 Task Detail 视图（进度、subtask、DAG、interaction log、output files）
* 右侧栏顶部有返回按钮回到辅助面板列表

### 视觉增强

* 卡片: `rounded-xl` + `shadow-sm` + `p-5`（更圆润+微阴影+更宽松）
* 标题: `text-base font-semibold`，去掉 uppercase + tracking-wide
* Header: `backdrop-blur-md` + `bg-surface/80` + `sticky top-0 z-40`
* 选中态: 左边框高亮 (`border-l-2 border-blue-500`) + 背景微变
* 空状态: 灰色 SVG 图标 + 引导文案，替代纯文字
* 过渡动画: `transition-all duration-200` 加到卡片 hover、折叠展开、主题切换

### 清理

* 删除死代码: TaskSubmitForm.tsx, ConnectionIndicator.tsx

## Acceptance Criteria

* [ ] Dashboard 在 1920x1080 下双栏布局，左8右4，信息密度合理
* [ ] 右侧栏面板可折叠/展开，折叠时显示关键指标摘要
* [ ] 点击 Task 右侧栏切换为 Task Detail 视图
* [ ] 卡片 rounded-xl + shadow-sm + p-5，标题 text-base font-semibold
* [ ] Header sticky + backdrop-blur
* [ ] 空状态有图标+引导文案
* [ ] hover/折叠/主题切换有 transition-all duration-200
* [ ] Dark/Light 主题切换后视觉一致
* [ ] 移动端布局可用 (1 列)
* [ ] 无死代码

## Definition of Done

* `cargo check` / `npm run build` 通过
* Dashboard 可正常构建和运行
* 无视觉回归 (dark/light 两主题)

## Out of Scope

* 新增路由系统
* 引入 shadcn/ui 或其他 UI 库
* 后端 API 变更
* 国际化

## Decisions

* **视觉风格**: 宽松现代 — 类似 Vercel/Linear Dashboard
* **布局方案**: 双栏主次布局 (8/12 + 4/12)
* **面板折叠**: 右侧栏辅助面板默认折叠，标题+关键指标摘要
* **Task 详情面板化**: 点击 Task 右侧栏切换为 Task Detail，返回按钮回到面板列表
* **卡片**: rounded-xl + shadow-sm + p-5
* **标题**: text-base font-semibold，去掉 uppercase
* **Header**: sticky + backdrop-blur
* **选中态**: border-l-2 + 背景微变
* **空状态**: SVG 图标 + 引导文案
* **过渡**: transition-all duration-200

## Technical Notes

* 关键文件: App.tsx (布局), index.css (主题+CSS变量), card.tsx (基础组件), Header.tsx
* 面板文件: TasksPanel, WorkersPanel, HealthPanel, CircuitBreakerPanel, EventLogPanel, SearchPanel, FileBrowser, SchedulerPanel
* 图表: TaskTrendChart (自定义 SVG)
* 死代码: TaskSubmitForm.tsx, ConnectionIndicator.tsx
* Task Detail 已有组件: components/panels/TaskDetail.tsx
