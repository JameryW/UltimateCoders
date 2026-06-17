# TUI polish — visibleLines自适应、LogoBanner清理、StatusBar去重

## Goal

修正 ChatLog 可见行数计算，使其随终端宽度自适应 LogoBanner 高度；清理 LogoBanner 重复逻辑；消除 StatusBar 常量重复。

## Requirements

* P0: visibleLines 自适应 — 根据终端宽度动态计算 LogoBanner 实际占用的行数（>=80: 6行, 60-79: 1行, <60: 0行），而非硬编码 -12
* P1: LogoBanner 清理 — 移除 compact prop（App 不传，内部已按宽度自适应），合并 <80 cols 分支和 compact 分支的重复 JSX
* P2: StatusBar 去重 — 删除 StatusBar.tsx 中独立的 MAX_RETRY_DISPLAY 常量，统一使用 statusbar-utils.ts 的导出

## Acceptance Criteria

* [ ] visibleLines 在 >=80 cols 终端下与当前值一致（-12）
* [ ] visibleLines 在 60-79 cols 下正确反映 compact logo（-7）
* [ ] visibleLines 在 <60 cols 下正确反映隐藏 logo（-6）
* [ ] LogoBanner 无 compact prop
* [ ] StatusBar 使用 statusbar-utils 的 MAX_RETRY_DISPLAY
* [ ] 332+ tests pass, typecheck clean

## Definition of Done

* Typecheck + tests green
* 手动验证不同终端宽度下 ChatLog 行数正确

## Out of Scope

* 新功能添加
* LogoBanner 视觉重新设计

## Technical Notes

* 关键文件：App.tsx (visibleLines), LogoBanner.tsx (compact/width logic), StatusBar.tsx (MAX_RETRY_DISPLAY)
* LogoBanner 当前逻辑：>=80 cols → 5行logo + 1行version = 6行; 60-79 → 1行compact; <60 → hidden
* visibleLines 当前：`max(5, rows - 12)` — 12 = logo(6) + separator(1) + statusIndicator(1) + input(1) + status(1) + borders(2) = 12
* 正确逻辑：`max(5, rows - (logoHeight + 6))`，其中 logoHeight 根据宽度变化
