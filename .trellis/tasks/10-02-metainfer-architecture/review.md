# MetaInfer 集成后的架构审查

日期：2026-10-02。基准：main `2b22e9adadb3def4f3ffb34e7f6afa8075524f48`。

UC 负责规划、派发、工作区和验收，MetaInfer 负责专业执行，这一分工适合继续保留。下一步应优先统一控制状态的写入规则、远端退出证明和共享资源排队，然后补齐运维与多机部署能力。本次审查未修改仓库已跟踪文件。

## 1. P1：普通任务快照仍能覆盖 Gateway 控制状态

`TaskStore::update_task` 直接写父任务和已有节点状态。NATS 完整快照已有取消/暂停与 attempt 保护，但这个 RPC 写入路径没有相同限制。OMP 的普通同步和显式重试都调用 `upsertTask` → `UpdateTask`，其中普通同步是异步发送，迟到快照可能重新打开已取消的任务。

隔离内存 TaskStore 的实际复现结果：

```text
cancelled parent/node/attempt: Failed/Failed/0
upsert parent/node/attempt: InProgress/Pending/0 (requested attempt 7)
```

已有节点的 `dispatch_retry_count` 也没有被这个 upsert 更新。重试重新开放节点但沿用旧 attempt，无法正确区分旧执行的迟到结果。

建议：把计划/投影更新与 Pause、Cancel、Resume、Retry 控制指令分开；计划更新不能覆盖控制状态。Retry 由 Gateway 原子创建新 attempt，更新 epoch/version 并保留事件证据。所有入口使用同一个状态转换模块。执行图迁移应在这些规则统一后分阶段推进，当前 `UC_GRAPH_SHADOW` 的默认关闭状态需继续明确记录。

验收：取消/暂停后迟到的 gRPC、NATS、OMP 快照都无法重新开放任务；Retry 产生新 attempt；旧 attempt 的部分结果和完整快照都被拒绝；重启恢复保留这些规则。

证据：[状态写入](C:/Users/jamer/aiworks/UltimateCoders/crates/uc-grpc/src/server.rs:1430)、[节点 upsert](C:/Users/jamer/aiworks/UltimateCoders/crates/uc-grpc/src/server.rs:1457)、[OMP 桥接](C:/Users/jamer/aiworks/UltimateCoders/packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts:557)、[异步同步](C:/Users/jamer/aiworks/UltimateCoders/packages/uc-orchestrator/src/orchestrator/orchestrator.ts:1872)。

## 2. P1：取消响应尚不足以证明远端写入进程退出

UC 收到 `kill` 的 `ok=true` 后，立刻记录 stopped 并释放后端槽位；优化流程随后可以回滚工作区。

兼容版本 MetaInfer `b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890` 中，控制路由返回 launcher.kill 的布尔值；底层只发送进程组信号，不等待退出。子 agent 的停止是 best-effort。force kill 还会直接把退出标记写入元数据，因此单独读取 finished 状态也不是全部写入进程退出的证明。由此推断，存在远端迟写与 UC 回滚/后续执行重叠的窗口；本次未在真实 GPU 服务中制造该故障。

建议：定义 cancel-and-wait 合约，确认所有工作区写入进程已退出，或取得服务端可验证的写入 fencing/quiescence 证明，再释放资源。无法证明时保留 cleanup_pending、租约、槽位和原始候选代码。

验收：停止请求成功但写入进程仍存活时，UC 不回滚、不释放；停止证明失败时隔离；证明齐全后只释放一次。覆盖真实上游与延迟停止故障服务。

证据：[UC 取消处理](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/inference/adapter.py:319)、[上游信号发送](https://github.com/HuangPuStar/MetaInfer/blob/b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890/metainfer/server/proc.py#L281)、[上游 launcher](https://github.com/HuangPuStar/MetaInfer/blob/b3f6505a11ab704ee1cfb68e9c1b2c13c95ac890/metainfer/server/launcher.py#L389)。

## 3. P1：共享后端繁忙被当作执行失败

Worker 能力表示插件可用，Gateway 的容量判断主要面向 Worker。多个 Worker 指向同一 MetaInfer 时，还会竞争一个共享后端预算。Adapter 的 reserve 在没有槽位时立即抛异常，runner 默认把它作为可重试执行失败；资源等待被归入执行失败，启用自动重试时会消耗任务重试额度。

在私有 SQLite 和 HTTP MockTransport 中预先占满一个后端槽位，第二个任务实际输出：

```text
MetaInferError: MetaInfer concurrency budget exhausted
HTTP calls: ['GET']; active slot retained: {'operation_id': 'active-job'}
```

建议：引入明确的资源等待/延期派发状态，区分 Worker 执行容量、MetaInfer 后端容量及实际 GPU 资源。等待容量不消耗执行重试额度，且能取消、超时和公平排队。共享后端需要稳定的资源身份，避免 URL 别名分裂预算。

验收：两个 Worker、一个后端槽位时，第二个任务等待，首个完成后执行；不产生失败事件；等待取消释放自身排队状态；Worker 重启不会重复占槽。

证据：[槽位不足处理](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/inference/adapter.py:176)、[runner 错误分类](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/inference/runner.py:351)。

## 4. P2：隔离状态缺少受控恢复与运维界面

submission_unknown、cleanup_pending、跨主机孤儿租约保留资源是正确的保护，但目前恢复依赖操作者检查并手工协调，没有统一的受控 reconcile 入口。实验列表接口已存在，Dashboard 尚未消费它形成实验/隔离视图，指标也缺少 outbox 等待时间和后端槽位占用。

建议：提供查询、附着已知远端任务、确认退出后恢复/放弃的受控命令，使用版本校验和审计记录。Dashboard 展示远端 ID、占用资源、隔离原因、Oracle verdict、accepted 与 delivered 的区别，并增加等待时间/隔离数量/重放积压指标。禁止只依据过期心跳自动清租约。

验收：丢失 POST 响应、失联 Worker、取消确认失败均可通过受控流程恢复；并发操作者不会重复释放资源；每次操作都有远端证据及审计。

证据：[隔离租约](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/agent/workspace.py:108)、[实验接口](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/dashboard/app.py:225)、[现有运维规则](C:/Users/jamer/aiworks/UltimateCoders/docs/inference-infra.md:123)。

## 5. P2：运行记录模块需要索引、类型和保留策略

RuntimeState 每次调用建立新连接，记录以通用 namespace + TEXT JSON 存储。每个 Worker 每五秒读取全部 result_outbox 再筛选；实验查询也先读取全部记录。没有删除/归档接口。历史增长后，读取和重放成本会随 Worker 数量和记录数量增长。

建议：为 RemoteJob、Experiment、WorkspaceLease、Outcome 提供有状态约束的接口，集中转换规则；使用连接复用、待发送记录索引、分页、延期重试和交付 claim。已交付数据采用保留/归档策略，同时保留去重墓碑，防止旧 dispatch 重投后再次执行。

验收：十万条已交付记录下，重放仅读取有限待发送页；多个 Worker 不重复抢同一交付；归档后旧 dispatch 仍保持幂等；PostgreSQL 和 SQLite 行为一致。

证据：[RuntimeState](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/runtime_state.py:37)、[全量重放扫描](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/nats_worker.py:2930)。

## 6. P2：多主机工作区和制品需要显式部署合约

UC 与 MetaInfer 依赖同一绝对路径的共享工作区；Compose 的制品 named volume 只解决同机共享。Dashboard 根据本机文件路径下载，远端 metadata 已存在而文件未共享时仍返回不可用。

建议：在提交前验证工作区共享及写入身份，使用稳定制品 ID、哈希和存储位置描述；落实跨机共享、复制或受控下载协议。把制品发布完成作为可观测交付状态，避免把 metadata 写入等同于制品已可下载。

验收：GPU Worker 与 Dashboard 分居两台主机时，能执行、验收并下载同一哈希的报告和 patch；共享工作区配置错误在远端任务启动前被检测。

证据：[共享工作区约束](C:/Users/jamer/aiworks/UltimateCoders/docs/inference-infra.md:15)、[跨主机制品约束](C:/Users/jamer/aiworks/UltimateCoders/docs/inference-infra.md:100)、[本机路径下载](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/dashboard/app.py:265)。

## 7. P2：基准环境指纹与 GPU 独占仍需加强

环境指纹记录声明配置、Python/platform、可见设备索引和 harness 哈希，但没有自动采集 GPU UUID、实际硬件型号、驱动和 CUDA/runtime/compiler 版本。统计稳定性检查不能替代物理资源识别和测试隔离。

建议：在基线和候选验证全过程保持设备预约；采集实际硬件、依赖版本及模型/权重身份；记录时钟、温度和竞争负载等测量条件，必要时采用交替复测，避免系统漂移被当作优化收益。

验收：实际设备或驱动改变能被环境校验发现；并发 GPU 任务不能污染验收；声明硬件与实际设备不符会失败；基线与候选的统计及环境证据完整。

证据：[环境指纹](C:/Users/jamer/aiworks/UltimateCoders/python/ultimate_coders/inference/benchmark.py:180)。

## 8. P2：补上真实 MetaInfer/GPU 合约验证

本轮之前 CI 已恢复；现有可靠性测试覆盖 HTTP 故障服务、本地 Ollama、NATS/Gateway 和存储，但已有验证报告明确记录外部 MetaInfer/GPU 优化尚未贯通。schema 探测只能说明字段接口存在，不能证明共享文件、GPU 调度、取消和输出导入可用。

建议：增加固定 MetaInfer 版本的联调环境，覆盖至少一次真实优化→保护基准→Oracle 接受/拒绝→回滚→提交交付，以及取消、重启和多主机报告下载。把版本升级与合约测试关联，GPU 测试作为 staging/release 验证。

验收：记录实际版本、硬件、远端任务 ID、完整测量样本、patch/commit 和制品哈希；拒绝与故障路径同样有证据。

证据：[现有验证范围](C:/Users/jamer/aiworks/UltimateCoders/docs/metainfer-reliability-verification.md:75)。

## 建议推进顺序

1. 修复三个 P1，并把隔离复现转成调用真实接口的回归测试。
2. 补齐受控 reconcile、运行记录查询/重放和 Dashboard 运维信息。
3. 落实跨主机工作区/制品合约、GPU 实测身份和真实 MetaInfer 联调。

实现时把规则集中在 Gateway 控制模块、MetaInfer 远端作业模块和运行记录模块，减少 Worker、runner、OMP 调用者分别了解状态转换和恢复顺序的负担。
