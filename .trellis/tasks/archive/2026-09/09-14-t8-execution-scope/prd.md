# T8: ExecutionScope — scope 校验/图行强化/派发过滤/worker projects 注册 (#650)

## 背景

P1 地图 #644 决议 D8 (#645)：ExecutionScope = `project_id` 形式化。Scope 即任务级
`project_id`（多仓库推迟到 P2）；本票把它从"约定字段"升级为"硬边界"：

1. **Submit 校验**：两条提交入口（gRPC `TaskService::submit_task`、NATS
   `uc.task.submit` 消费）都要求 `project_id` 非空（trim 后非空，拒绝空白）；
   创建后不可变（update 路径不得改写 scope）。
2. **图行强化**：`execution_graphs.project_id`（T4 已有列 + 索引）活跃写入口
   `upsert_task_shadow` 拒绝空 scope；legacy 回填路径保持原样（历史空行由
   open-worker 语义兜底）。
3. **Worker projects 注册**：`RegisterWorkerRequest.projects`（repeated string，
   空 = open worker 接任何 scope）；Python 侧 `UC_WORKER_PROJECTS`（逗号分隔）
   注入注册。
4. **派发硬过滤**：`dispatch_gate(required, project_id)` —— capability 匹配
   之后、contract_version 闸之前按 scope 过滤；scoped worker 永不收 foreign
   scope 节点，open worker 全收，空 scope 任务只给 open worker。

## 验收

- 空/空白 project_id 提交在两条入口都显式报错（gRPC 返回 error 响应；Python
  消费者 log+丢弃）。
- scope 不匹配时子任务保持 Pending（不出 Assigned、不 publish），新 gate 变体
  `NoScopeMatchedWorker` 可观测。
- worker 注册携带 projects 并存入 registry；ListWorkers 原样（scope 仅内部用）。
- Rust 基线只增不减；pytest 对应文件绿。

## 非目标

- 多仓库 scope（P2）；per-worker 派发 subject（T12 #654 affinity 才引入）；
  merge/context/env 三票（T9–T11）。
