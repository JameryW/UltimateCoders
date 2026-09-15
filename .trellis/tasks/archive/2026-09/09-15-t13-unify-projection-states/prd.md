# T13: 统一两份图投影的依赖感知规则（#655）

## 背景

`graph_nodes.state` 的 `Pending` 语义在图平面里是**有歧义**的：它既覆盖"已知但还不可运行"（`CREATED`），也覆盖"现在可运行"（`READY`），只有依赖集能区分。T2（#638，提交 `28b66e9`）为 Rust 形态写了依赖感知的 `node_status_of_subtask`，但**同一次提交里**为 TS 形态写的 `project_ts_task` 仍走纯 token 映射，于是同一条规则在两条投影路径上不一致。

## 目标

让**三条**图投影路径对同一逻辑图产出**逐字节相同**的 node states，并让这条不变量被测试真正钉住（而不是只写在测试注释里）。

## 范围

**在范围内**

1. 抽出依赖感知判定的单一实现，`project_task` 与 `project_ts_task` 共用（不再有第二份可漂移的副本）。
2. `project_ts_task` 建立 TS 侧 status 索引并走同一规则。
3. 补测试：
   - TS `pending` + 依赖未完成 → `CREATED`；
   - TS `pending` + 依赖全 `completed` → `READY`；
   - 跨源等价：同一逻辑图分别经 `project_task` / `project_ts_task` → states 相同（**覆盖 pending**）。
4. 修正模块头 L43-55 那张已失效的映射表（消除同文件内的第二条矛盾表述）。

**不在范围内**

- 不改写路径语义（`shadow=false` 的 `DO NOTHING`、`graph_exists` skip、`UC_GRAPH_IMPORT_DIR` opt-in 均保持）。
- 不引入生产侧读 `graph_nodes.state`（今天没有消费者，`node_state()` 仅测试调用）。
- 不动 `transition_ok` / 9 状态机 / T3 写动词。

## 验收

- [ ] `project_task` 与 `project_ts_task` 对同一逻辑图 node states 逐字节相同（含 pending-with-unmet-deps）
- [ ] 新增测试做了突变自检（撤掉修复必须变红，且报告实际红/绿数字）
- [ ] `cargo fmt --all -- --check` 绿
- [ ] `cargo clippy -p uc-engine --all-features -- -D warnings` 绿
- [ ] `cargo test -p uc-engine` 默认模式：基线只增不减（原 lib 440+5）
- [ ] 模块文档与实现一致，无第二条矛盾表述

## 风险

**低。** 导入路径 opt-in 且 insert-only，无生产读取方；行为变化仅限"从 `.uc/tasks` 迁移进来的图，其阻塞节点由 `READY` 改判 `CREATED`"——即向 source A / shadow 的既有语义对齐。
