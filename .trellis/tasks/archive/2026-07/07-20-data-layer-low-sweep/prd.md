# PRD: Data Layer LOW Sweep — Round 16 审计收尾（#8-#11）

## 背景

/loop 第 18 轮。round 16 数据层审计剩 4 项 LOW，全收 → 该审计清单清零。

## Bug 清单（已核实）

### F48: buildDAG 重复 id 误导诊断（审计 #8，S）

LLM decomposer 发重复 subtask id（模型生成 JSON，可能）→ detectCycles 返回 `[]` → `if (cycles)` 对空数组为真 → 抛 "Circular dependencies detected: []"（无 id、错因）；重复 dependsOn 条目 → inDegree 多算（dependents Set 去重只减一次）→ Kahn 死锁 "Deadlock in dependency graph"。均抛错不挂起，但诊断送用户去查不存在的环。

修：buildDAG 入口显式 id 唯一性校验（抛 "Duplicate subtask id ..."）；inDegree/dependents 构建用去重后的 dependsOn（`new Set(st.dependsOn)`），校验循环照旧。

### F49: recursiveDecompose 波序错（审计 #9，S，潜在无生产调用方）

children[1..n] 在循环内 push 进 result，含 children[0] 的 newWave 循环后才 push → 波序 `[c1],[c2],...,[c0+siblings]`，而 c1..cn dependsOn c0（文件分裂）或链式 ci→c(i-1)（描述分裂）→ 依赖未完成即开始。文件分裂的独立 children 还被串成单元波而非并行。

修：children[0] 占父位（newWave），其余收集后按依赖小 Kahn 拼在父波之后——dependsOn 已由已发波满足者同波并行（文件分裂 c1..cn 仅依赖 c0 → 并行），链式自然串行。emitted 集 = result 已发波 + newWave 全部 id。

### F50: rpc 协议面杂项（审计 #10，S×4）

1. dispatch 一律 -32000 → 未知方法 -32601、缺参 -32602（RpcError 类，dispatch 按 instanceof 取 code，余 -32000）。
2. parse-error 响应 `id: 0` 与合法客户端 id 0 冲突（req.id ?? ++nextId 保留客户端 0）→ JSON-RPC 规范 `id: null`；JsonRpcResponse.id 改 `number | null`。
3. not-found 形状分歧：show_status `{status:"not_found"}` vs get_task `{task:null}` → 两边补全为超集 `{status:"not_found", task:null}`（客户端兼容）。
4. `params.subtask_id as string | undefined` 透传非字符串（数字等）→ 永不匹配 → 假 not-found。改：定义但非 string → RpcError -32602 "subtask_id must be a string"。

### F51: task id 文件名消毒（审计 #11，S，防御性）

task-store 各方法以 id 裸拼路径。当前不可达（id 服务端生成 `uc-<n>-<ts>`，调用方皆内存/磁盘来源），但外部 id 路径一旦出现，`../` 逃逸 tasks 目录。

修：私有 `assertSafeId`（`/^[\w.-]+$/` 且不含 ".."），save/load/remove/saveCheckpoint/loadCheckpoint 入口调用。

## 验收

- scheduler.test.ts：重复 id → "Duplicate subtask id" 错误；重复 dependsOn 不死锁（波正常）；recursiveDecompose 文件分裂 → 父波含 c0、次波 c1/c2 **并行**同波；链式分裂 → 严格序。
- 新 uc-rpc-server.test.ts（stub orchestrator）：未知方法 -32601；缺参 -32602；get_task 未找到 `{status:"not_found", task:null}`。
- task-store.test.ts：`../evil` id save 抛 Unsafe。
- tsc src/ 零错误（**pwd 验证包目录**）；bun test test src 全绿。
- feature branch + PR + CI green。

## 意义

Round 16 数据层审计 11 finding 至此**全部完成**（#1-#11）。/loop 总清单剩零——下轮起转 Rust gRPC / Python worker 审计（CLAUDE.md 架构面从未审计）或停 /loop（需用户决策）。
