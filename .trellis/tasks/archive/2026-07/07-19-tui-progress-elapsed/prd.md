# PRD: Progress Widget Elapsed + No-Data Percent

## 背景

/loop 第 9 轮。审计剩 #6（progress-widget elapsed，MED/S-M）+ #10（0% 无数据混淆，LOW/S）。#6 可行性第 7 轮已确认（零协议变更，client-side firstSeen）。

## 改

### F19: progress-widget running 行 elapsed（审计 #6）

`SubtaskProgressInfo` 无时间戳，且 extension subtask_progress handler 每次全量替换条目。progress 事件仅来自远程 worker（WatchTask）——本地 subtask 根本无 progress 条目，widget tag 行不渲染。

1. `SubtaskProgressInfo` 加 `firstSeen?: number`。
2. **subtask_start 播种**（F14 已在该 handler 创建 ps）：`progressBySubtask.set(subtaskId, { phase: "starting", percent: -1, firstSeen: Date.now() })`——elapsed 从 dispatch 起算，本地 subtask 也有条目。
3. **subtask_progress 携带**：`info.firstSeen = prev?.firstSeen ?? Date.now()`（续传不重置）。
4. **tag fitter 渲染**：`(${formatElapsed(now - firstSeen)})` tag，优先级 parallel 之后、phase 之前（agent>pct>step>status>parallel>elapsed>phase）——预算紧时 phase 先砍，elapsed 次之。
5. `formatElapsed` 抽共享 `ui/elapsed.ts`（subtask-tree F8 已有一份同逻辑私有函数，两处 = 漂移风险；status-icons 教训：4× 重复才提取，2× 带注释尚可——但本处完全同语义同格式 `(42s|3m|1h 05m)`，直接共享），subtask-tree 改 import。

**限制（明示）**：widget 仅事件驱动重绘（setWidget on events），elapsed 在两次 progress/lifecycle 事件间冻结。远程 worker progress 频繁（秒级）→ 近似实时；本地 subtask 仅在 start/end 刷新。全局 1s setWidget timer 会令 chat 区整 TUI 每秒重绘（任务运行可达数十分钟），成本远超 overlay modal 场景（F7），不做。

### F20: percent 无数据 ≠ 0%（审计 #10）

orchestrator handleWatchTaskEvent L375-376：`d.percent ?? "0"` + `Number(x) || 0` → worker 未报 percent 时 widget 显 "0%"，与真实 0% 不可区分。

修：缺失/空串 → `-1`（widget `percent >= 0` guard 已跳过负数，无需改 widget）；顺带 clamp `Math.max(0, Math.min(100, …))` 防 worker 越界。

## 验收

- progress-widget.selfcheck.ts：firstSeen 65s 前 → tag 行含 `(1m`；percent -1 → 无 `0%`/`-1%` tag；elapsed 优先级低于 parallel（窄宽先丢 elapsed 保 parallel）。
- subtask-tree selfcheck 全绿（formatElapsed 共享重构无行为变更，F8 断言不变）。
- bun test test src + tsc（src/ 零错误）+ feature branch + PR + CI green。

## 不做

- 全局 setWidget 1s timer（成本/收益不合算，见限制）。
- #12 error 分类词边界、#13 小打磨（Wave 标签/lastRender 死字段/id 预算）、#9 deps 后缀、clipboard（下轮候选）。
