# Dashboard: Worker detail expansion + TaskTrendChart polish

## Goal

让 WorkersPanel 展示 worker 当前任务和详情；修复 TaskTrendChart 数据整合问题。

## Requirements

1. WorkersPanel expand-on-click: 显示 worker 完整 ID、heartbeat 时间、当前负载详情、capabilities 列表
2. TaskTrendChart: 添加 Legend，修复 bucket 逻辑（按小时聚合而非分钟字符串 key）

## Acceptance Criteria

- [ ] WorkersPanel 点击 worker 展开详情（full ID, heartbeat, load, capabilities）
- [ ] TaskTrendChart 有 Legend 组件
- [ ] TaskTrendChart 按小时正确聚合（非分钟级字符串碰撞）

## Out of Scope

* URL routing（大改，单独做）
* Auth（部署前处理）
* Time range selector（后续改进）
* Task submit auto-scroll
