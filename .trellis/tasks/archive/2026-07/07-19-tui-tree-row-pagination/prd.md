# PRD: Subtask-Tree Row-Based Pagination

## 背景

/loop TUI 优化第 5 轮（Explore 审计 #4，M effort）。subtask-tree `render()` 按 **item 数** 分页（`items.slice(scrollOffset, scrollOffset + maxVisible)`），但展开项最多渲染 **3 行**（base + error/result + meta）。

后果：maxVisible=12 时若多 item 展开，总行数可达 ~36，超 overlay `maxHeight:"100%"` clamp——compositor 静默截掉底部 footer（X-Y of Z + ▲▼）与 flashMsg 行。正是 overlay-pagination 重写要消灭的失败模式，expand 路径绕过了它。

task-list overlay 每 task 恒 1 行（age 折进主行），分页已行精确，不动。

## 改（subtask-tree-overlay.ts 单文件）

### F9: 行预算窗口

1. `itemLineCount(item)`: `1 + expanded ? (error||result ? 1 : 0) + (hasMeta ? 1 : 0) : 0`。hasMeta = retryCount>0 || review || (dispatchMode && ≠prefer_remote)——meta 行存在性与 width 无关（width guard 只丢 tag 不清空行）。
2. render 窗口循环：从 scrollOffset 累计 itemLineCount 至 maxVisible 预算；`used + n > budget && visible.length > 0` 时 break（不切半个 item；首项恒收，≤3 行 ≪ 预算）。记 endIdx。
3. footer：条件改 `scrollOffset > 0 || endIdx < items.length`（旧 `items.length > maxVisible` 在 item 少但全展开时漏显）；range 用 endIdx；▲/▼ 同 F6 语义。
4. clampScroll：item 窗口算术失效。backward（cursor < offset）照旧 offset=cursor；forward 改 `while (offset < cursor && !fitsInWindow(cursor)) offset++`（cursor 每按一键移 1 item，循环 ≤ 几次）。`fitsInWindow(target)`: 从 offset 累计行数，target item 起始处超预算即 false（首项豁免，mirror render）。
5. PgUp/PgDn 保持 item 跳 ±maxVisible，clampScroll 兜底修正。

## 验收

- selfcheck（24-row tui，maxVisible=12）：
  - 6 subtask 全展开（各 3 行=18 行）→ render 只显示 4 item（12 行预算），footer "1-4 of 6 ▼" **存在**（旧代码 item-slice 全收 6 项 18 行，footer 被 clamp 截掉）。
  - cursor 移到 item 5 → clampScroll 推进 offset，该 item 行可见 + footer 更新。
  - 全折叠回归：50 项 → 1-12 of 50（同旧行为）。
- 既有 selfcheck 全绿（F6 箭头、filter、retry）。
- bun test test src + tsc（src/ 零错误）。
- feature branch + PR + CI green。

## 不做

- task-list 不动（已行精确）。
- overlay-pagination RESERVED_ROWS 调整（footer/flash 预留已在 12 行 chrome 预算内；行窗口是正解，非调常量）。
- reconnect footer、progress-widget elapsed、clipboard（下轮候选）。
