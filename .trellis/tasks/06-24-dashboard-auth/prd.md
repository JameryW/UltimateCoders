# Dashboard 功能完善：auth 验证 + 数据映射修复 + 错误可见性

## Goal

修复 Dashboard 中 3 类已知问题：auth 假验证、gRPC 数据映射错误、静默错误吞没。让安全层和数据显示层真正可靠。

## What I already know

* `useAuth.login()` 永远返回 true，不验证密码 — 安全假象
* `useAuth` mount 时用 raw gRPC-Web fetch 做健康检查，任何非 0 status 都认为 authenticated
* **Proto 实际字段**：
  * `CircuitBreakerProto`: `state`, `failureCount`, `failureThreshold`, `recoveryTimeoutSeconds`, `lastFailure` — **没有** `total_calls` 或 `total_rejected`
  * `RateLimiterProto`: `maxRequests`, `windowSeconds`, `currentRequests`, `remainingRatio` — **没有** `total_requests`，但有 `remainingRatio`
* 当前映射问题：
  * `failureThreshold` → `total_calls`（语义错误，阈值≠总调用）
  * `maxRequests` → 同时映射到 `rpm_available` 和 `total_requests`（RPM gauge 永远 100%）
  * `tpm_available` 硬编码 0
  * `total_rejected` 硬编码 0
* `engine_circuit_breaker` / `engine_rate_limiter` 已 fetch 但从未渲染
* 5 个 `catch {}` 静默吞错误
* `night_window.timezone` 永远空字符串
* 后端 2 个遗留死文件

## Requirements

### R1: Auth 验证修复

* `login(password)` 调用 gRPC Health（带 Bearer token）
* 验证成功才设 `isAuthenticated = true`，失败显示错误信息
* 登录中显示 loading 状态
* mount 时如已有 token，用 token 调 Health 验证（token 可能已过期）

### R2: Rate Limiter 数据映射修复

* `maxRequests` → `total_requests`（窗口内容量上限）
* `currentRequests` → `active_count`（当前占用）
* `rpm_available` = `maxRequests - currentRequests`（剩余容量）
* `remainingRatio` 直接用于 RPM gauge 百分比显示
* `windowSeconds` 渲染在 UI 中（告知用户窗口大小）
* 移除 `tpm_available` 硬编码 0，改为从 proto 取或标记不可用

### R3: Circuit Breaker 数据映射修复

* `failureThreshold` → 保留为阈值字段（不再映射到 `total_calls`）
* 新增 `failure_threshold` 字段到 `CircuitBreakerInfo`
* `total_calls` 和 `total_rejected`：proto 不提供，标记为不可用（不显示或显示 N/A）
* `recoveryTimeoutSeconds` 和 `lastFailure` 加入类型并渲染

### R4: Engine CB/RL 指标渲染

* CircuitBreakerPanel 新增 engine metrics 区块
* 渲染 `engine_circuit_breaker` / `engine_rate_limiter` 的关键字段

### R5: 静默错误可见化

* `listTasks` / `health poll` / `getRepos` 等关键 catch 不再静默
* 重复失败时 toast 或 stale indicator
* SSE parse error → `console.warn`

### R6: 清理

* 删除后端遗留死文件（templates/index.html, static/dashboard.js）
* `night_window.timezone`：proto 和后端都不提供，移除 SchedulerPanel 中 timezone 显示

## Acceptance Criteria

* [ ] 错误密码登录显示错误信息，不进入 Dashboard
* [ ] 已存储 token 失效时自动跳回登录页
* [ ] RPM gauge 使用 `remainingRatio`，显示真实使用率
* [ ] CircuitBreaker `failureThreshold` 显示为 "阈值: N"，`total_rejected` 不再硬编码 0
* [ ] Engine CB/RL 指标在 CircuitBreakerPanel 可见
* [ ] gRPC 持续失败时 UI 有可见提示
* [ ] 后端死文件已删除
* [ ] tsc --noEmit 通过

## Definition of Done

* Lint / typecheck green
* 无新 silent catch 块引入

## Out of Scope

* 重构 Dashboard 组件架构
* 添加新 panel
* Auth token refresh / expiry 机制
* TUI 侧修改
* 后端新增 proto 字段（仅用现有字段修复前端映射）

## Technical Approach

### R1: Auth

当前 `useAuth` mount 时 raw fetch gRPC-Web Health，login 不验证。
改为：
1. mount 时如有 token → 用 `useGrpcWeb.healthCheck()` 验证 token
2. login 时 → 存 token → healthCheck() → 成功才 `isAuthenticated=true`
3. healthCheck 失败 → 清 token，显示错误

### R2: Rate Limiter

用 `remainingRatio`（0-1）直接驱动 RPM gauge。
`total_requests` = `maxRequests`，`active_count` = `currentRequests`。
`rpm_available` = `Math.round(maxRequests * remainingRatio)` 或 `maxRequests - currentRequests`。

### R3: Circuit Breaker

`CircuitBreakerInfo` 增加 `failure_threshold`, `recovery_timeout_seconds`, `last_failure`。
`total_calls` / `total_rejected` 不再硬编码，标记 `available: false` 或显示为 N/A。

### R6: 死文件

```bash
rm python/ultimate_coders/dashboard/templates/index.html
rm python/ultimate_coders/dashboard/static/dashboard.js
```

## Technical Notes

### Proto 字段（已确认）

**CircuitBreakerProto**: state, failureCount, failureThreshold, recoveryTimeoutSeconds, lastFailure

**RateLimiterProto**: maxRequests, windowSeconds, currentRequests, remainingRatio

### 关键文件

| 文件 | 修改点 |
|------|--------|
| `dashboard/src/hooks/useAuth.ts` | R1 |
| `dashboard/src/hooks/useDashboardGrpc.ts` | R2/R3 映射修复 |
| `dashboard/src/types/dashboard.ts` | R2/R3 类型扩展 |
| `dashboard/src/components/panels/CircuitBreakerPanel.tsx` | R4 engine metrics |
| `dashboard/src/components/panels/SchedulerPanel.tsx` | R6 timezone |
| `dashboard/src/App.tsx` | R5 错误可见化 |
| `python/ultimate_coders/dashboard/` | R6 死文件 |
