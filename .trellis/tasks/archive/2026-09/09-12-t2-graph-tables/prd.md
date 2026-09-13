# T2: 图状态行表 + 一次性导入

Tracker: #638（blocked by #637 已关；本票解锁 T3 #639）
Design refs: assessment §4 D1/D2 约束 1、§3 G1；research/pg-storage-current-state.md

## Goal

把"整 Task JSONB blob"规约成图关系表并建立导入通道，为 T3 状态机提供 schema 与数据底座。**行为不变**：内存 HashMap 仍是运行时权威，本票只做表、影子写、导入器与影子读 diff。

## Scope

1. **`crates/uc-engine/src/graph_store.rs`（新，storage feature）**，五张表：
   - `execution_graphs`（graph_id PK=task_id、project_id、status、version、root_scope 预留 ''、created/updated_at）
   - `graph_nodes`（node_id+graph_id 复合 PK、type/state、dependencies JSONB、dependency_policy、scope_id 预留、input_refs/output_refs JSONB、priority、deadline、optional、**effect_class 默认 'requires_worker'**、required_capabilities JSONB、version）
   - `task_attempts`（attempt_id PK、node/graph FK、worker_id、**worker_epoch**、status、retry_no、started/heartbeat/finished_at、result_ref、**UNIQUE(node_id, retry_no)**）
   - `execution_events`（graph/node/attempt、seq BIGSERIAL、graph_version、event_type、payload JSONB、**预留 cost/tokens/duration_ms**——评估件遗留风险的清零点）
   - `node_completions`（node_id PK、winning_attempt_id、result_ref、committed_at）——**只有 schema + 唯一约束**，commit-once 逻辑归 T3。
   - 迁移严格走 `scheduler/migration.rs` 样板：`hold_schema_migrations_lock(pool, "graph")` + IF NOT EXISTS + 非 storage stub。env 用 `UC_DATABASE_URL`（与 task backend 同源；`UC_PG_URL/UC_DATABASE_URL` 分裂记入 spec 待 P1 归一）。
2. **一次性回填器**（幂等，`ON CONFLICT DO NOTHING`，startup 挂钩于 uc-grpc-server main.rs 装配点）：
   - 源 A：PG `tasks` JSONB → 图表（每次 graph 迁移后自动跑一遍；重跑无感）。
   - 源 B：`.uc/tasks/*.json`——仅当 `UC_GRAPH_IMPORT_DIR` 显式设置且 PG 无对应 graph 才导入（D2 约束 1"不再双向"）；task 文件 vs checkpoint 取 savedAt 较新者。
   - 状态映射：Completed→SUCCEEDED（同事务写 node_completions 行）、InProgress/Assigned→RUNNING（attempt 行带 started_at）、Pending→READY、Failed→FAILED、Cancelled→CANCELLED、Conflicted→FAILED；TS camelCase→Rust snake 字段转换在导入器内。
   - 每图 `version=1` 起，`imported=true` 标记列。
3. **影子写 + 影子读**（feature env `UC_GRAPH_SHADOW=on`，默认 off）：`persist_task` 旁路 upsert 图表；启动 load 完成后对每个 task 比对行表投影与 HashMap，diff 只 `tracing::warn`。**不改任何读路径行为。**

## Out of scope

状态机/CAS/commit-once 逻辑（T3）、Nats-Msg-Id/去重（T4）、Executor（T5）、TS 反转（T6）、agent_events 旧表清理（记 follow-up）。

## Test seams（真 PG，全部门禁）

- 连接：`UC_PG_URL_TEST` 用 **127.0.0.1**；构造后 **assert is_connected()**（防假绿）；测试库 DROP 用 `WITH (FORCE)`。
- 回填幂等：同库跑两遍行数不变；`.uc/tasks` fixture（含 checkpoint 较新场景）导入后 graph/node/attempt/completion 计数与状态映射逐断言。
- 确定性：同输入导入两次 byte-identical（对 result_ref 之外的列投影比对）。
- 排序二义：行表 ORDER BY 与内存投影排序一致性测试（跨后端一致只有跨后端测试能抓——评估件教训）。
- 冷启动并发迁移：双 process 同时 startup 撞 graph 迁移不炸（复用 #631 测试模式）。
- 影子 diff：人为制造分歧断言只 warn 不改行为。
- 无真 PG 环境下全套 skip（对齐现有 ci 守卫：skip 必须可见，不许静默绿）。

## 基线数字

uc-engine 428 / no-default 370 / uc-grpc 180+8 / uc-grpc-server 35 / uc-types 28；pytest 全量 977。新测试只加不减。
