# Add TUI Interactive Entry in Dashboard

## Goal

在 dashboard 独立页面中提供 TUI 交互入口，通过 PTY + WebSocket 桥接 OMP 终端 session，让用户在浏览器中获得完整终端体验。

## Requirements

* Dashboard 新增独立 TUI 页面（`/tui` 路由），全屏 xterm.js 终端
* 后端 spawn 单全局 PTY 进程运行 OMP，WebSocket 端点桥接 PTY stdin/stdout
* 单 session 模式：全局一个 PTY，断开可重连，PTY 保持运行
* 认证复用 dashboard 现有 Bearer token 机制
* Header 中增加 TUI 入口按钮/链接

## Acceptance Criteria

* [ ] Dashboard Header 有 TUI 入口，点击跳转 `/tui` 页面
* [ ] `/tui` 页面全屏 xterm.js 终端，连接后端 PTY
* [ ] 可执行 /uc 命令（submit/status/cancel/pause/resume）
* [ ] 快捷键可用（Ctrl+T, Ctrl+Shift+T）
* [ ] TUI 渲染与 OMP 终端内一致
* [ ] 断开 WebSocket 后重连可恢复 session
* [ ] 认证：未登录跳转 auth，Bearer token 验证 WebSocket

## Definition of Done

* Lint / typecheck / CI green
* 手动验证 TUI 页面功能
* Docs/notes updated if behavior changes

## Out of Scope

* 多 session / 多用户隔离
* TUI 面板内嵌回 dashboard（作为可选 tab/panel）
* PTY 进程自动重启（手动管理即可）

## Technical Approach

### 架构

```
Browser (/tui page)
  └─ xterm.js ──WebSocket──> FastAPI /ws/tui ──PTY──> OMP process
```

### 关键组件

1. **FastAPI WebSocket 端点** (`/ws/tui`)
   - 启动时 spawn 单全局 PTY（`node-pty` 或 `ptyprocess`），运行 OMP
   - WebSocket 收到数据 → 写入 PTY stdin
   - PTY stdout → 推送到 WebSocket
   - 认证：从 query param `?token=` 或首条消息验证 Bearer token
   - 断开不杀 PTY，重连继续

2. **Dashboard TUI 页面** (`/tui`)
   - 新增 React route（简单 hash route 或 react-router）
   - xterm.js + xterm-addon-fit（自适应尺寸）
   - WebSocket 连接管理（断线重连、auth token 注入）
   - 全屏布局，Header 简化版（返回 dashboard 按钮 + 连接状态）

3. **Header 入口**
   - 现有 Header 组件加 TUI 按钮/链接

### 依赖

* `xterm` + `@xterm/addon-fit` (前端)
* `ptyprocess` 或 `pexpect` (Python PTY) — FastAPI 端
* `fastapi[websocket]` — 已有

## Decision (ADR-lite)

**Context**: TUI session 运行位置选择
**Decision**: PTY + 真实终端（方案 2），spawn 全局 PTY 进程跑 OMP
**Consequences**: 完整终端体验，但需要 PTY 进程管理；单 session 限制 MVP 够用

## Technical Notes

* Dashboard: `dashboard/src/` — React 19, Vite 8, Tailwind 4, 无路由库
* OMP TUI: `packages/uc-orchestrator/src/ui/` — 6 个 pi-tui 组件
* FastAPI: `python/ultimate_coders/dashboard/app.py` — ~1583 行，SSE 端点
* Auth: `dashboard/src/hooks/useAuth.ts` — Bearer token, localStorage `uc_dashboard_token`
* Vite proxy: `/ultimate_coders.*` → `:50051`，需加 `/ws/tui` proxy 规则
* 无现有 WebSocket 代码
