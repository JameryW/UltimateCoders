# PRD: Tool Bridge Polish — Round 12 审计 S 项收尾（#11-#17）

## 背景

/loop 第 14 轮。round 12 审计剩 7 项（#11 M + #12-#17 S），全收。

## 清单（已核实）

### F33: task id 补全（审计 #11，M）

getArgumentCompletions（extension.ts L430）只补全子命令首词。`/uc cancel <Tab>` 无候选——id 长且只显示截断，补全是仅次于前缀匹配（F26 已做）的可用性项。

修：第 2 词起且首词 ∈ status/cancel/pause/resume → 返回 `orchestrator.getAllTaskStates()` 的 id（按当前词前缀过滤，slice 10，label 带 status）。cancel 第 3 词（subtask）不做（低频）。

### F34: uc_worker prefix 多匹配静默取首（审计 #12，S）

worker-bridge.ts L105 `find(id === wid || startsWith)` → "worker" 匹配 worker-1/worker-2 静默取 worker-1，LLM 可能按错误 worker 调度。修：filter 收集，>1 → 报错列全部匹配要 full id。

### F35: uc_memory schema/描述缺口（审计 #13，S）

- key 描述未说明 search 时它是查询词（LLM 传字面 key 得 "(no results)"）。
- importance 描述 0-1 但 schema 无约束；content_type 收任意字符串（server 深处不透明报错）。
- write 失败裸 "Write failed" 无因。

修：key 描述加 "(or search query for action=search)"；content_type 改 enum（text/structured/code/diff/reference）；importance 加 .min(0).max(1)；write !ok 消息补 "(server rejected or unavailable)"。

### F36: index_repo 失败无因 + 无路径预检（审计 #14，S）

bridge.indexRepo 返回 bool，失败无因（真错误仅 console.warn）。修：客户端 statSync 预检 local_path（不存在/非目录 → 明确错误，覆盖最常见 typo 场景）；!ok 消息补 "(server rejected or unavailable — check gateway logs)"。bridge 错误透传需改桥签名，不做（ponytail 上限注释）。

### F37: list_dir 空目录 vs 不存在混淆（审计 #15，S）

file-bridge.ts L31 "(empty directory or not found)"——agent 探路分不清 typo 与真空。修：无 repo_id（本地 fs）→ statSync 区分 not found / not a directory / empty；有 repo_id（远程）→ 消息改 "(empty directory, or not found in remote repo)"（远程无法客户端 stat，诚实表述）。

### F38: spawn-disabled 消息中文改英文（审计 #16，S）

extension.ts L448 + task-bridge.ts L47 中文消息，其余全英文。改英文（保留 UC_NO_SPAWN 变量名）。

### F39: header 注释漏 /uc search（审计 #17，S）

extension.ts 文件头命令列表补 `/uc search <query>` 行。

## 验收

- task-bridge.test.ts：spawn-disabled 消息含英文 "UC_NO_SPAWN"（既有测试断言 toContain("UC_NO_SPAWN") 保持绿）。
- tsc src/ 零错误（enum/min/max schema 编译验证）；bun test test src 全绿。
- feature branch + PR + CI green。

## 不做

- #4 bridge 控制动词误归因（M，需桥返回结构改造，下轮）。
- /uc search path 复制（低价值，toast 瞬逝）。
- 此后 round 12 审计清零；转新一轮审计或 Rust/Python 面。
