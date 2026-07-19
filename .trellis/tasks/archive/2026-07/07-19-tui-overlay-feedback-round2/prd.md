# PRD: TUI Overlay Feedback Round 2

## 背景

/loop TUI 交互优化第 2 轮调研（Explore agent 审计 packages/uc-orchestrator/src/ui/）。S1-S11 已 done（PR #284-#292），本轮修 4 个新发现的 overlay 反馈缺口，全部 S effort、同主题（反馈一致性）。

## Bug 清单

### F1: detail mode flashMsg 从不渲染（render 层回归，静默废掉 S3/S4）

`task-list-overlay.ts` `render()` L126 有 detailTaskId 就走 `renderDetail()`，而 renderDetail **不输出 flashMsg 行**（renderList L192-195 有）。detail 所有反馈路径（`/` L257、c/p/r L263-274 → fireAction → setFlash）都设了状态但用户看不见。S4 selfcheck 只断言状态变量不断言渲染输出，所以漏过。

修：`renderDetail(width)` 末尾补 flashMsg 行（mirror list mode），render() 传 width。

### F2: fireAction 失败时 flash 停留/缺失

- list mode 双击 c：L381 设 `flashMsg = "cancelling…"`，fireAction `.then` 只在 ok=true 设成功 flash（L414-415）。ok=false → "cancelling…" 永留到下次按键。
- sync 分支同理：ret falsy 无反馈。

修：`.then(ok => ok ? setFlash(verb) : setFlash(\`${action} failed\`))`；sync 分支 else 补 failed flash。

### F3: p/r 无 onAction 时静默死键

list mode p/r（L391-397）只 `if (task && onAction)`，否则无反馈；c 有 "cancel unavailable"（L388-389）。detail mode c/p/r 走 fireAction，onAction 缺失时 L407 静默 return。

修：list p/r 补 `else if (task) flashMsg = "pause/resume unavailable"`；fireAction onAction 缺失时 setFlash（list 侧调用前已 guard，不会双闪；补 detail 侧缺口）。

### F4: subtask-tree filter 不匹配 status

`subtask-tree-overlay.ts` currentItems（L112-116）只匹配 id + description；姊妹 task-list overlay 含 status（task-list-overlay L107-112）。Ctrl+T 树里打 `/ failed` 返回 no match，尽管有 failed subtask——最常见过滤动机。

修：predicate 加 `it.subtask.status.toLowerCase().includes(q)`。

## 验收

- 4 个 selfcheck（task-list-overlay.selfcheck.ts + subtask-tree-overlay.selfcheck.ts）覆盖：
  - F1: detail mode 动作后 **渲染输出** 含 flash 文本（非仅状态）。
  - F2: onAction 返回 false → "${action} failed" flash。
  - F3: 无 onAction 时 list p/r + detail c/p/r 有 unavailable flash。
  - F4: tree filter status 命中。
- bun test（或 selfcheck 运行方式）全绿。
- tsc -p packages/uc-orchestrator 干净。
- feature branch + PR + CI green。

## 不做（下轮 /loop 候选）

- #2/#6 定时刷新 timer（age 冻结、reconnect 倒计时、elapsed 显示）— M effort，独立主题。
- #4 pagination 按行数不按 item 数（expand 溢出）— M。
- #9 滚动 ▲▼ 提示、#10 unknown status fallback — S 但异主题，下轮。
- #5 clipboard 复制 — 依赖 host API，待评。
