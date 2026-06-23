# Dashboard UI 完善 + 页面布局和视觉效果优化

## Goal

完善 UltimateCoders Dashboard 的功能完整性和视觉体验，使其从一个功能可用但视觉粗糙的监控面板，升级为专业、美观、信息密度合理的运维 Dashboard。

## What I already know

* React 19 + TypeScript + Vite 8 + Tailwind CSS v4
* 单页应用，无路由，hash 滚动导航
* 4 列网格布局 (xl) / 2 列 (md) / 1 列 (mobile)
* 当前组件: Header, TasksPanel, WorkersPanel, HealthPanel, CircuitBreakerPanel, EventLogPanel, SearchPanel, FileBrowser, SchedulerPanel, TaskTrendChart
* CSS 变量主题系统 (dark/light)，shadcn/ui 模式但手写组件
* Card 组件: rounded-lg border bg-surface p-4
* CardTitle: text-sm uppercase tracking-wide (很小的标题)
* 颜色系统完整 (badge/status/evt/btn 全有 dark+light 变体)
* 自定义 SVG 图表 (TaskTrendChart)，无 recharts 使用
* 虚拟滚动 EventLog (@tanstack/react-virtual)
* Mermaid DAG 渲染 + DOMPurify
* highlight.js 代码高亮 (FileBrowser)
* 死代码: TaskSubmitForm.tsx (未使用), ConnectionIndicator.tsx (未使用)

## Assumptions (temporary)

* 不引入新的 UI 库 (保持手写 shadcn 模式)
* 不引入路由库 (保持 hash 滚动)
* 优化在现有 Tailwind + CSS 变量体系内完成
* 优先视觉效果和布局，其次新功能

## Open Questions

* 布局优先级：哪些面板最重要，应该占据更显眼的位置？
* 视觉风格偏好：更紧凑密集 vs 更宽松现代？
* 需要新增哪些功能面板或交互？

## Requirements (evolving)

* 改善 Header 视觉层次和品牌感
* 优化 Card 组件视觉 (间距、阴影、圆角)
* 改善面板布局分配 (信息密度和重要性匹配)
* 增强交互反馈 (hover、transition、微动画)
* 改善空状态和加载状态视觉
* 清理死代码

## Acceptance Criteria (evolving)

* [ ] Dashboard 在 1920x1080 下信息密度合理，无大片空白
* [ ] 所有面板 hover/交互有一致的视觉反馈
* [ ] Dark/Light 主题切换后视觉一致
* [ ] 移动端布局可用 (1 列)
* [ ] 无死代码 (未使用的组件/导入)

## Definition of Done (team quality bar)

* Lint / typecheck / CI green
* Dashboard 可正常构建和运行
* 无视觉回归 (dark/light 两主题)

## Out of Scope (explicit)

* 新增路由系统
* 引入 shadcn/ui 或其他 UI 库
* 后端 API 变更
* 国际化

## Technical Notes

* 关键文件: App.tsx (布局), index.css (主题), card.tsx (基础组件), Header.tsx
* 面板文件: TasksPanel, WorkersPanel, HealthPanel, CircuitBreakerPanel, EventLogPanel, SearchPanel, FileBrowser, SchedulerPanel
* 图表: TaskTrendChart (自定义 SVG)
* 死代码: TaskSubmitForm.tsx, ConnectionIndicator.tsx
