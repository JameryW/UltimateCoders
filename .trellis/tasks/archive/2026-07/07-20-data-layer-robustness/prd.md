# PRD: Data Layer Robustness — 持久化/控制订阅 5 修

## 背景

/loop 第 16 轮，新审计（从未审计的数据层：task-store / control-signal-subscriber / scheduler / uc-rpc-server）挖出 11 finding。本轮取 HIGH + 关联 MED/S 簇（#1/#2/#3/#4/#7）。#5（checkpoint 新鲜度，M）、#6（路径归一，darwin 大小写真 bug，S/M 需设计）下轮；#8-#11 LOW 再下轮。

## Bug 清单（已核实）

### F41: 一个坏任务文件恢复时吞掉全部任务（审计 #1，HIGH）

task-store.ts loadAll（L88-104）整个读循环单个 try/catch——任一 `.json` 解析失败 → 循环中断，catch 返回 `[]`，**已解析的任务也全丢**。restore() 建在其上 → 所有 in-progress/paused 任务从 TUI/dashboard 消失一整轮，仅 console.warn。

修：try/catch 移入循环内——坏文件跳过 + warn，其余保留。

### F42: 非原子写制造坏文件（审计 #2，HIGH，#1 之因）

save/saveCheckpoint 直写最终路径——SIGKILL/OOM 中断留截断/空文件（不可解析）。与 F41 叠加：一次中途崩溃 → 下次启动丢**所有**任务。RPC server 被父进程 kill 是现实场景。

修：同目录写 `${file}.tmp` 再 `fs.rename` 覆盖（POSIX 原子）。removeStale 顺带清 `.json.tmp` 孤儿（崩溃残留）。loadAll 天然跳过（`.endsWith(".json")` 不匹配 `.tmp`）。

### F43: 轮询回落永不检测 resume——无 NATS 时 paused 任务永卡（审计 #3，HIGH）

checkControlStateChange（L263-289）只处理 Paused/Failed 转移。无 NATS（回落路径，本地开发常态）时，远程 resume 把 status 移出 "Paused" → 轮询观察到转移却两个分支都不进 → resumeTask 永不调用 → 任务卡 paused 直到 NATS 恢复或重启。文件头注释承诺 pause/resume/cancel。

修：加分支 `previous === "Paused" && currentStatus !== "Paused" && currentStatus !== "Failed"` → handler.resumeTask。

### F44: stop() 落在 connect() 窗口内 → 订阅者复活泄漏（审计 #4，MED）

start()（L84）与 tryNatsReconnect()（L156）`await connect()` 后不查 stopped。stop() 在 ≤2s connect 窗口执行 → 见 natsConn===null 干净完成 → pending connect 随后赋值 natsConn + startNatsSubscription → 泄漏 NATS 连接 + 迭代循环活到进程结束。正是 stopped 标志要防的跨会话泄漏。

修：两处 `await connect(...)` 后 `if (this.stopped) { close conn; null; return; }`。

### F45: resume_task 绕过 UC_NO_SPAWN（审计 #7，MED/S）

uc-rpc-server submit_task 有 isSpawnDisabled 闸门（L142-144），resume_task 无。UC_NO_SPAWN 下 resume 进入 wave 执行，每个 subtask 派发才失败——任务空转全部 wave 产出失败 subtask 终 failed，而非前置拒绝（pause/cancel 不派发，唯 resume 需闸门）。

修：resume_task case 加同款 isSpawnDisabled 守卫。

## 验收

- 新 task-store.test.ts：坏文件 + 好文件 → loadAll 返回好文件（F41）；save 后无 `.tmp` 残留、内容正确（F42）；saveCheckpoint 同。
- control-signal-subscriber.test.ts：checkControlStateChange Paused→Running → resumeTask 调用；Paused→Failed → cancel 逻辑不变（经既有/新断言）。
- tsc src/ 零错误（**pwd 验证包目录**）；bun test test src 全绿。
- feature branch + PR + CI green。

## 不做（下轮）

- #5 checkpoint vs task 文件新鲜度（M）：restore 无条件偏 checkpoint，mid-wave 崩溃后重跑已完成 subtask。
- #6 文件冲突匹配裸字符串相等（S/M，darwin 大小写真 bug）：./src/a.ts vs src/a.ts、src/a.ts vs src/A.ts 判不冲突 → 同 wave 并发写同文件。
- #8-#11 LOW：buildDAG 重复 id 误导诊断、recursiveDecompose 波序（潜在无调用方）、rpc 错误码/形状、task id 文件名消毒（当前不可达）。
