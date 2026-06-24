# Dashboard Sparkline 趋势图与指标导出

## Goal

在 v3 MetricsPanel 基础上，渲染后端 trend 采样数据的 sparkline 趋势图（内联 + 全宽面板），增加指标导出 CSV 功能。

## Requirements

### R1: 内联 Sparkline（指标卡片嵌入）

每个 MetricsPanel 区域的关键指标右侧嵌入 80×24px 微型折线图：

- System Overview: cluster_utilization 趋势线
- Task Efficiency: avg_duration_ms 趋势线 + error_rate 趋势线
- Worker Performance: cluster_load_pct 趋势线
- Event Flow: events_per_minute 趋势线

数据源：`metrics.trend[]`，纯 SVG polyline 渲染。

### R2: 全宽趋势面板（MetricsTrendChart）

在 MetricsPanel 4 卡下方新增全宽趋势图面板：
- 一个 SVG 内绘制 4 条趋势线（events_per_minute, avg_duration_ms, error_rate, cluster_utilization）
- 带 X 轴时间标签、Y 轴刻度、图例
- Hover tooltip 显示采样点详情
- 与 `TaskTrendChart.tsx` 风格一致

### R3: 指标导出 CSV

MetricsPanel 顶部增加 "Export CSV" 按钮：
- 导出内容：当前 metrics 快照（所有字段）+ trend 时间序列
- 文件格式：CSV，UTF-8 with BOM（Excel 兼容）
- 纯前端实现：Blob + URL.createObjectURL + `<a download>`

## Acceptance Criteria

- [ ] MetricsPanel 各区域关键指标右侧有内联 sparkline
- [ ] MetricsPanel 下方有全宽 MetricsTrendChart 趋势图
- [ ] 趋势图 hover 显示采样点详情
- [ ] 趋势线无数据时显示空占位/空图
- [ ] "Export CSV" 按钮下载包含快照 + trend 的 CSV
- [ ] tsc + eslint clean

## Definition of Done

* Lint / typecheck / CI green
* Empty state handled gracefully

## Technical Approach

1. 新建 `dashboard/src/components/charts/Sparkline.tsx` — 通用迷你折线图组件（纯 SVG polyline）
2. 修改 `MetricsPanel.tsx` — 各区域指标值旁嵌入 `<Sparkline>`
3. 新建 `dashboard/src/components/charts/MetricsTrendChart.tsx` — 全宽多线趋势图（参照 TaskTrendChart 风格）
4. 修改 `MetricsPanel.tsx` — 底部嵌入 `<MetricsTrendChart>` + 顶部 Export CSV 按钮
5. CSV 导出用 `exportMetricsCsv()` 工具函数

## Out of Scope

* 告警规则配置 UI
* 指标对比面板
* 后端指标持久化
* 第三方图表库

## Technical Notes

* `TaskTrendChart.tsx` 是纯 SVG 图表先例
* `MetricsSample` 类型在 `dashboard/src/types/dashboard.ts`
* 项目约定：纯 SVG，不引入图表库
