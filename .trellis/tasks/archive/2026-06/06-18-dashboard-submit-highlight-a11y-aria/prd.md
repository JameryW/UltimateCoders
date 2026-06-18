# Dashboard: post-submit highlight + A11Y aria labels

## Goal

提交任务后自动高亮新任务；补全关键组件的 ARIA 属性和键盘支持。

## Requirements

1. TaskSubmitForm 提交成功后，通知 App.tsx 新建的 taskId → TasksPanel 自动展开该任务并滚动到可见
2. TaskDetail/InteractionLog 补全 aria-label、role、键盘支持
3. Mermaid SVG 容器加 role="img" + aria-label

## Acceptance Criteria

- [ ] 提交任务后 TasksPanel 自动展开新任务并滚动到可见
- [ ] TaskDetail 的 subtask filter select 有 aria-label
- [ ] InteractionLog 列表有 aria-label + aria-live
- [ ] Mermaid SVG 有 role="img" + aria-label

## Out of Scope

* URL routing
* Auth
* 全局 command palette
