# Dashboard Polish: Light Theme + Auth UX + FOUC Fix

## Goal

修复 Dashboard 的 Light 主题兼容性问题、Auth UX 缺口、和初始加载闪烁，使所有面板在 dark/light 两种模式下都正确显示。

## What I already know

* Auth gate (useAuth + LoginModal + 后端 _check_auth) 已实现
* Dark/Light toggle (useTheme + CSS 变量 + Header 按钮) 已实现
* URL hash routing 已实现
* 所有面板功能完整（Health, Workers, Tasks, EventLog, Scheduler, CB, Chart, Search）

## Assumptions (temporary)

* 不需要添加新功能面板
* Light theme 的配色方案沿用 index.css 已定义的 CSS 变量

## Open Questions

* (无阻塞问题 — 所有问题都可以从代码中推导)

## Requirements

1. **Light theme 兼容**: 所有组件改用 CSS 变量替代硬编码 `bg-dark-*` / `text-gray-*` 颜色
2. **Badge light theme**: index.css 中的 `badge-*` 类需要 light 模式变体
3. **Header Logout 按钮**: 在 Header 添加 logout 按钮（调用 useAuth.logout）
4. **FOUC 修复**: index.html 添加 `data-theme` 属性，阻止初始加载闪烁

## Acceptance Criteria

- [ ] Light 模式下所有面板文字/背景可读（无硬编码 dark 主题色）
- [ ] Light 模式下 badge 颜色正确（ok=green, error=red 等）
- [ ] Header 有 logout 按钮，点击后清除 token 并显示 LoginModal
- [ ] 初始加载无闪烁（index.html 带 data-theme 属性）
- [ ] `pnpm build` 无报错

## Definition of Done

* Lint / typecheck / build 通过
* 两种主题下目视验证

## Out of Scope

* 新功能面板
* JWT/OAuth
* 角色权限
* 响应式布局重构

## Technical Notes

### 硬编码 dark 主题色的文件清单

| File | Hardcoded Classes |
|------|------------------|
| `TaskSubmitForm.tsx` | `bg-dark-800`, `bg-dark-900`, `border-dark-700`, `text-gray-200`, `text-gray-300` |
| `EventLogPanel.tsx` | `bg-dark-700`, `border-dark-600`, `text-gray-300`, `placeholder-gray-500` |
| `WorkersPanel.tsx` | `bg-dark-700`, `text-gray-300` |
| `TaskDetail.tsx` | `bg-dark-900`, `border-dark-700`, `text-gray-300` |
| `InteractionLog.tsx` | `text-gray-300` |
| `TasksPanel.tsx` | `text-gray-300`, `bg-dark-700` |
| `SearchPanel.tsx` | `bg-dark-900`, `border-dark-700`, `text-gray-200` |
| `LoginModal` (App.tsx) | `bg-[var(--bg-surface)]` (已用 CSS 变量 ✓), input 用了 `bg-[var(--bg-primary)]` ✓ |

### 替换策略

- `bg-dark-800` → `bg-[var(--bg-surface)]`
- `bg-dark-900` → `bg-[var(--bg-primary)]`
- `bg-dark-700` → `bg-[var(--bg-surface-alt)]`
- `border-dark-700` / `border-dark-600` → `border-[var(--border-color)]`
- `text-gray-300` (正文) → `text-[var(--text-primary)]`
- `text-gray-400` (标签) → `text-[var(--text-secondary)]`
- `text-gray-500` (弱文字) → `text-[var(--text-muted)]`
- `placeholder-gray-500` → `placeholder-[var(--text-muted)]`

### Badge light theme

需要在 index.css 中为 `[data-theme="light"]` 添加 `badge-*` 变体，使用更深的背景色和深色文字。

### FOUC 修复

index.html 的 `<html>` 标签需要加 `data-theme="dark"` 默认属性。这样 CSS 变量在 JS 加载前就已生效。
