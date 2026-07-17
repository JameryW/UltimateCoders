# PRD: Dashboard SSE Reconnect Backoff

## 背景

dashboard agent 审计 finding #3。`useDashboardGrpc.ts` SSE fallback 重连无退避。

## Bug

`scheduleReconnect` (L308-330)：
- L309 `delay = RETRY_INTERVALS[retryCountRef.current] ?? MAX_RETRY_INTERVAL`（指数退避 1→60s）
- L315 `retryCountRef >= SSE_FALLBACK_THRESHOLD(5) && !usingSseRef` → 转 SSE
- L317-321 `setTimeout(connectSse, 1000)` — **固定 1000ms，不用 delay**

SSE error handler (L381-388) 设 `usingSseRef=false` + `scheduleReconnect()`。retryCount 已 >=5 且 usingSseRef false → L315 再 connectSse，固定 1s。

**结果**：双故障（gRPC 挂 + SSE 端点 404/不存在，如 gRPC-only 部署）下，SSE 失败→1s→SSE→失败→1s… 无退避狂打 SSE 端点。retryCount 涨但 L321 不读 delay。

gRPC path (L325) 用 delay 有退避。SSE path 不对称。

## 改

L321 `1000` → `delay`（复用 L309 退避）。SSE 重连也指数退避到 MAX 60s。

## 验收

- 逻辑推理：SSE 失败→scheduleReconnect→delay 涨→SSE 重连间隔增长。
- vite build + `tsc -p tsconfig.app.json` useDashboardGrpc.ts clean（CI tsc no-op，见 [[dashboard-ci-tsc-noop]]）。
- feature branch + PR。
- PR 后查 CI。

## 不做

- 不改 SSE_FALLBACK_THRESHOLD 或 RETRY_INTERVALS 值。
- 不加 SSE 成功重置 retryCount（SSE 成功即稳定，不断不调 scheduleReconnect；保持现状）。
- 不碰 Rust / SSE 服务端。
