# PRD: Dashboard Trend Extended Unavailable Hint

## 背景

dashboard agent 审计 finding #11。`MetricsPanel.tsx` 选 6h/24h 时 fetch `/dashboard/api/trend`，失败静默用 SSE 1h 数据冒充。

## Bug

L287-294：fetch trend，`res.ok ? res.json() : null`，catch 静默。失败（端点 404/网络错/Python dashboard 缺失）→ extendedTrend null → L313 `trend = effectiveTrend ?? metrics.trend` = SSE 1h 数据。**用户选 24h，看 1h 数据，无提示**——误导。

端点存在（app.py L723），仅无 Python dashboard 部署或临时故障触发。但静默冒充是真 UX 缺口。

## 改

- 加 `extendedFailed` 状态（boolean）。
- fetch 非 ok 或 catch → setExtendedFailed(true)。trendRange 变化重置（effect 内）。
- UI：trendRange > 60 且 extendedTrend null 且 extendedFailed → trend 图下 dim 提示 "showing last 1h — extended data unavailable"。

## 验收

- vite build + `tsc -p tsconfig.app.json` MetricsPanel clean（L56/57 预存 error 非我改）。
- feature branch + PR。
- PR 后查 CI。

## 不做

- 不改端点或 SSE。
- 不碰 Rust。
