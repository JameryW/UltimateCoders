# PRD: TUI Widget/Footer Lifecycle Fixes

## 背景

/loop 第 7 轮，新 Explore 审计（progress-widget/status-formatter/extension 事件面）挖出 13 finding。本轮取**相互耦合的 widget/footer 生命周期簇**（#1/#11/#4/#8），同两文件、同主题。#7 终态 footer 保留（"UC: completed" 是末任务结果展示，非 bug；真 bug 是 #4 的 planning 永驻）。

## Bug 清单（全部已对当前代码核实）

### F11: orchestrator 纯文本 widget 覆盖 extension 富 widget（审计 #1，HIGH）

orchestrator.ts `updateWidget()`（L1950）写纯 `string[]` 到 `uc-${task.id}`——与 extension.ts 富组件工厂**同 key**（L136/152/179）。OMP per-key map last-writer-wins → 每 wave 序列：subtask 事件装富 widget → `wave_end` 前 L629 纯文本覆盖，直到下个 subtask 事件。纯文本块还无 width 感知、icon 表漂移（无 planning）、受 MAX_WIDGET_LINES=10 截。

修：删 3 处 updateWidget 调用（L567/580/629）+ widgetKey 局部量 + updateWidget 方法本体（无其他调用者，死码清除）。widget 归 extension 独占。

### F12: wave_end 不渲染 + reviewing 丢 live tag（审计 #11）

extension.ts wave_end handler（L182-186）只 updateProgressState，无 setWidget → wave 行陈旧（F11 前靠被覆盖的纯文本"显"）。subtask_reviewing 与 end/failed 同分支（L141-155）删 progressBySubtask 条目 → review 期间 agent/step/percent tag 全消失。

修：wave_end 补 ps.task 快照 + setWidget（mirror subtask_start 模式）；reviewing 拆出——只刷新 task 快照，不删 progress 条目（end/failed 才删）。

### F13: decompose 失败遗留 planning 态（审计 #4，MED-HIGH）

task_planning 设 workingMessage "UC: Planning..." + footer "UC: planning" + progressState 条目。decompose catch（orchestrator.ts L427-433）标 failed + notify 后 return，**不发 task_complete** → 三者永驻/泄漏（仅 task_complete 清理，extension L187-193）。

修：catch 内补 `this.events.emit("task_complete", { taskId, status: "failed", summary: task.error })`（mirror L706 载荷），extension 既有 handler 清全部三样。

### F14: resumed/local 任务无富 widget（审计 #8）

subtask_start handler（L131-138）仅 `if (ps)` 刷新——ps 仅由 task_planning（新提交）/subtask_progress（仅远程 worker）创建。重启 resume 的任务（发 task_resumed 非 task_planning）+ 本地执行 subtask（无 progress 事件）→ 无富 widget（F11 前掉回纯文本，F11 后什么都没有）。

修：subtask_start 内 ps 缺失时创建（mirror subtask_progress L159-163）再装 widget。

## 验收

- 尽量加回归测试：真实构造 UCOrchestrator + stub pi/bridge，decompose 失败 → events 收到 task_complete{status:"failed"}。构造过重则降级：tsc + 既有 129 测试全绿 + 代码审查确认 emit 载荷同 L706。
- extension 侧无单测基建（需 pi host），靠 tsc + 逐 handler 模式对照。
- feature branch + PR + CI green。

## 不做（下轮候选，审计剩余）

- #2 task-result details.task 死码（closure getter 修）
- #3 status-formatter width 前缀溢出（desc width-15 / error width-9 + formatErrorForDisplay）
- #5 多行 error 拍平（classifyError rootCause newline→space）
- #6 progress-widget elapsed（firstSeen stamping，可行性已确认）
- #9 stIcon.length ANSI 宽误算、#10 0% 无数据、#12 error 分类误报、#13 小打磨
- clipboard（vendor copyToClipboard 已存在未导出，OSC52 验证安全）
