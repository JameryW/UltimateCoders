# Dashboard Theme Polish Round 2

## Goal

将所有面板组件中残留的硬编码 dark 主题色替换为 CSS 变量，确保 light/dark 两种模式下所有面板正确显示。

## What I already know

* PR #76/#77 已修复了 index.css CSS 变量、badge light 变体、Header logout、FOUC
* 当前 uncommitted changes 修复了 useDashboard/useSSE/App.tsx 的核心逻辑
* 面板组件仍有大量 `text-gray-*`, `bg-gray-*`, `border-l-gray-*`, `bg-dark-*` 硬编码

## Requirements

1. **text-gray-400** (标签) → `text-[var(--text-secondary)]`
2. **text-gray-500** (弱文字) → `text-[var(--text-muted)]`
3. **bg-gray-600/800** → `bg-[var(--bg-surface-alt)]` / `bg-[var(--bg-surface)]`
4. **border-l-gray-600** → `border-[var(--border-color)]`
5. **Status semantic colors** (text-green-400, text-red-400 等) — 保留语义色，但确保 light 模式下可读（用 CSS 变量或 theme-aware class）
6. **TaskTrendChart hex fallbacks** — 替换为 CSS 变量读取
7. **SearchPanel bg-purple-900/50, bg-cyan-900/50** — 替换为 theme-aware 变体

## Acceptance Criteria

- [ ] 所有面板无 `text-gray-*`, `bg-gray-*`, `border-gray-*` 硬编码
- [ ] Light 模式下所有面板文字/背景可读
- [ ] `pnpm build` 无报错

## Out of Scope

* 新功能面板
* 响应式布局重构
* JWT/OAuth
