# PRD: Restore Freshness + File-Intent Path Normalization

## 背景

/loop 第 17 轮。round 16 数据层审计剩 #5（M）+ #6（S/M，darwin 真 bug）。

## Bug 清单（已核实）

### F46: restore 无条件偏 checkpoint → mid-wave 崩溃重跑已完成 subtask（审计 #5，M）

restore()（orchestrator.ts L301-305）`source = cp ?? p` 无条件偏 checkpoint。但 saveCheckpoint 仅 wave 边界写（单一 checkpoint() 调用点），save/persist 每状态转移写（~20 处）。mid-wave-k+1 崩溃：task 文件含 wave k+1 已完成 subtask 0..m；checkpoint 是 wave k 末。从 checkpoint 恢复 → m 个已完成 subtask 回退 pending → resumeTask 重跑（在**已编辑过的文件**上再编辑——编辑在崩溃中存活 → 重复/冲突编辑 + 浪费 token）。

修：两件产物盖 `savedAt`（save/saveCheckpoint 写时 `Date.now()`，PersistedTask 加可选字段），restore 取**较新**：
- 两边都有 savedAt → 新者胜（task 文件通常更新 → mid-wave 进度保留）。
- legacy 无 savedAt checkpoint → 视作 Infinity（保持旧优先序，不破坏旧数据行为）。
- legacy 无 savedAt task 文件 → 0。

### F47: 文件冲突匹配裸字符串相等 → darwin 大小写变体判不冲突（审计 #6，S/M，真 bug）

scheduler hasFileOverlap（L248-252，splitWavesByFileOverlap 用）+ FileIntentTracker.isConflicting（L301-310）精确串比。decomposer files 来自 LLM 输出无归一保证：`./src/a.ts` vs `src/a.ts`、`src/a.ts` vs `src/A.ts`（**darwin 大小写不敏感 fs——本仓库跑 darwin**）、相对/绝对 → 全判不冲突 → 两 subtask 同 wave 并发写同文件 last-write-wins 损坏。

修：`normalizeFileIntent(file)` = `path.normalize`（去 `./`、压 `..`/重复分隔符）+ 平台感知 case fold（darwin/win32 toLowerCase）。hasFileOverlap 比较前 map normalize；FileIntentTracker.declare 存归一 key、isConflicting 归一查询（getOwnedFiles 显示归一 key——debug 用途可接受，注释说明）。

上限：不做 workspace-root 相对化（需 root 上下文，跨调用点传参重）；normalize + case fold 覆盖 LLM 输出的常见变体。

## 验收

- scheduler.test.ts：splitWavesByFileOverlap——`./src/a.ts` vs `src/a.ts` 判冲突分不同 sub-wave；darwin 下 `src/A.ts` vs `src/a.ts` 同（平台条件断言）。FileIntentTracker：declare `src/A.ts` → isConflicting `src/a.ts` 命中（darwin 条件）。
- task-store.test.ts：save 后 load 得 savedAt 定义。
- tsc src/ 零错误（**pwd 验证包目录**）；bun test test src 全绿。
- feature branch + PR + CI green。

## 不做

- #8-#11 LOW 杂项（下轮 sweep 收尾：buildDAG 重复 id 诊断、recursiveDecompose 波序、rpc 错误码/形状、task id 消毒）。
- workspace-root 相对化（F47 上限）。
