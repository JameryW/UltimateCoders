# PRD: TUI Overlay Affordance Round 3

## 背景

/loop TUI 优化第 3 轮。上轮（PR #305）F1-F4 done。本轮 2 个 S effort 显示层缺口（来自 2026-07-19 Explore 审计 #9/#10）。

## 缺口

### F5: unknown status 静默伪装成 plan/pending

- `status-icons.ts` L29：未知 status fallback 到 pending ○（progress-widget / subtask-tree / status-formatter / task-result-renderer 全走此共享函数）。
- `task-list-overlay.ts` L41 statusBadge：未知 fallback 到 planning "plan"。

后果：未来新增/改名的 status（server 侧先升级时）渲染成 "planning/pending"，误导用户以为任务没动。应显式标 unknown。

修：
- `STATUS_ICON` 加 `unknown: (t) => t.fg("dim", "?")`，fallback 改走 unknown；更新 L13 注释。
- `STATUS_BADGE` 加 `unknown: (t) => t.fg("dim", "?   ")`（4 字符列宽对齐其余 badge），fallback 改走 unknown。

### F6: 滚动截断无方向提示

两 overlay 仅数字 footer `X-Y of Z`（subtask-tree L266-268、task-list list L188-190 + detail L216-218）。scrollOffset>0 时用户看不出上方还有内容被截（必须读数字算）。

修：footer 加方向箭头 —— 上方有截断前缀 `▲ `，下方有截断后缀 ` ▼`（dim，同 footer 行，不增行）。task-list 抽 `scrollFooter(offset, visible, total)` 私有方法两处复用；subtask-tree 单处同模式。

## 验收

- status-icons.selfcheck.ts：unknown status → "?"（非 ○）；已知 status 不受影响。
- task-list-overlay.selfcheck.ts：unknown badge "?" 渲染 + 列宽；PgDn 后 footer 含 ▲、顶部含 ▼、中间双箭头。
- subtask-tree-overlay.selfcheck.ts：滚动后 footer 箭头。
- bun test test src + tsc clean。
- feature branch + PR + CI green。

## 不做（下轮）

- #2/#6 定时刷新 timer（M）— age 冻结 / reconnect 退避不可见 / running subtask elapsed。
- #4 pagination 按行数（M）— expand 溢出。
- #5 clipboard 复制（M，依赖 host API）。
