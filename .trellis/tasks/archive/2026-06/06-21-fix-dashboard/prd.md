# fix: dashboard 布局优化

## Goal

重新组织 Dashboard 面板布局，使视觉层级清晰、信息密度合理、核心操作突出。

## Requirements

* Tasks 面板升级为 2 列宽，SubmitForm 合并进 Tasks 面板头部
* EventLog 2 列（日志需要宽度）
* FileBrowser 引入主布局，2 列宽
* Health 从 2 列降为 1 列（组件状态列表不需要宽）
* Scheduler 1 列（信息密度低）
* ConnectionIndicator 并入 Header 右侧
- 移动端适配不被破坏

## Target layout (xl:grid-cols-4)

```
Row 1: Tasks(2,含SubmitForm) + Workers(1) + CB(1)
Row 2: EventLog(2) + Search(2)
Row 3: FileBrowser(2) + Health(1) + Scheduler(1)
Row 4: Chart(2)
```

## Acceptance Criteria

* [ ] xl 屏幕下 Tasks 占 2 列，SubmitForm 内嵌在 Tasks 面板顶部
* [ ] FileBrowser 出现在主布局中
* [ ] Health/Scheduler 各占 1 列
* [ ] ConnectionIndicator 移入 Header，不再 fixed 浮动
* [ ] md 屏幕下 2 列布局合理
* [ ] 移动端 1 列布局不破坏

## Out of Scope

* 面板内部 UI 重构
* 新增面板/功能
* 路由/导航重构

## Technical Approach

1. App.tsx: 重排 grid 子元素顺序和 col-span
2. TaskSubmitForm → 合并进 TasksPanel 顶部
3. ConnectionIndicator → 移入 Header 右侧
4. FileBrowser → 加入 grid
5. Header: 接收 ConnectionIndicator props

## Technical Notes

* 面板: HealthPanel, WorkersPanel, TasksPanel, SchedulerPanel, CircuitBreakerPanel, EventLogPanel, SearchPanel, TaskTrendChart, FileBrowser
* CSS: Tailwind + CSS 变量主题
* 路由: hash-based
