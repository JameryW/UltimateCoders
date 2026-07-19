# PRD: Dashboard Alert History Error Feedback

## 背景

dashboard agent 审计 finding #12。`alert-bar.tsx` fetchHistory 失败静默，显示 "No alerts recorded" 误导。

## Bug

L78-86：fetch `/dashboard/api/alerts?limit=100`，`if (res.ok)` 才 setAlertHistory。非 ok / catch 静默。alertHistory 空 → L138-139 显示 "No alerts recorded"——**用户无法区分「真无告警」vs「fetch 失败」**。

端点存在（app.py L705），仅无 Python dashboard 部署或临时故障触发。但静默误导是真 UX 缺口（同 #11 模式）。

## 改

- 加 `historyError` 状态（boolean）。
- fetchHistory：非 ok / catch → setHistoryError(true)；成功 → setHistoryError(false) + setAlertHistory。
- L138 empty state：historyError → "Alert history unavailable"；否则 "No alerts recorded"。

## 验收

- vite build + `tsc -p tsconfig.app.json` alert-bar clean。
- feature branch + PR。
- PR 后查 CI。

## 不做

- 不改端点。
- 不碰 Rust。
