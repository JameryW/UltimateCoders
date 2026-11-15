# 15 — 端到端 A-B 指标测量 (T15)

**What to build:** 一个可跑的测量脚本，从权威记录（`attempt_chain` +
`knowledge_evidence`）聚合出 spec-p0 的验收指标，并支持两份状态的**配对比较**
（同一任务集上 baseline 与 variant），输出 JSON 供评测/看板消费。

**Blocked by:** T04（补丁链）、T09/T12（结局与收据证据）、T13（签名归一化）
**Spec:** [spec-p0.md](../spec-p0.md#评测协议)
**Status:** implemented（`python/ultimate_coders/knowledge/metrics.py`；
CLI `python -m ultimate_coders.knowledge.metrics --state ... [--baseline ... --variant ...]
[--json] [--signatures]`；`tests/python/test_knowledge_metrics.py` 14 tests；
跑数步骤见 [../runbook-ab-evaluation.md](../runbook-ab-evaluation.md)）

**Implementation**

1. `collect_samples(state, *, max_records=5000)`：读 `attempt_chain` 轮次与
   `knowledge_evidence` 中的 `ATTEMPT_OUTCOME` / `FAILURE_SIGNATURE`，按 attempt
   身份连接；缺字段容错，结果确定性排序；无 token 用量时保持 `None`（不当 0）。
2. `compute_metrics(samples)`：按 `(task, subtask, dispatch_attempt)` 组链；
   `succeeded_subtasks` 取链末轮 success；`repeated_failures` 用归一化签名在链内
   首次之后重复计一次；导出 `success_rate`（4 位）、`avg_attempts` /
   `repeated_failure_rate` / `tokens_per_subtask`（2 位）。
3. `compare_metrics(baseline, variant)`：只配对**共同 task_id**；判定词表固定
   `insufficient_data`（配对 <2）、`regressed`（成功率下降）、`inconclusive`
   （tokens/subtask 恶化 >5%）、`improved`（attempt 数或重复失败率下降且成功率非劣、
   tokens 不劣）、否则 `non_inferior`。
4. CLI：单状态输出聚合 JSON（不带 `--json` 时输出人读表格）；`--baseline/--variant`
   输出比较 JSON；`--state` 接受 SQLite 路径或 `postgres://` URL（后者经
   `RuntimeState(database_url=)`）。
5. `signature_histogram(samples, *, limit=MAX_SIGNATURES)` + CLI `--signatures`：
   只统计失败且上报了签名的 attempt，按 `(-count, signature)` 排序，最多
   `MAX_SIGNATURES=50` 条；用于校准 `normalize_failure_signature` 的粗细度
   （配对 JSON 下输出 `{"baseline": [...], "variant": [...]}`）。

**Acceptance gates**

- [x] 聚合口径可复算（固定样例：tasks/subtasks/attempts/failures/repeated/
      succeeded/rates/tokens 全部断言）。
- [x] 无 usage 时 token 指标为 `None`/0，不伪造 0 成本。
- [x] 配对只用共同任务；样本不足返回 `insufficient_data`。
- [x] 判定与理由一致：`regressed` / `improved` / `non_inferior` / `inconclusive` 各有用例。
- [x] CLI 单状态与配对两种输出都是合法 JSON。
- [ ] 在真实 A-B 运行集上产出结果（脚本已就绪；数据采集需 stage B/C 实际跑任务）。

**Rollout:** 只读聚合，不写权威；可随时跑，默认不影响 worker。
**Risks:** token 指标依赖 agent 上报 usage；缺失时该维度诚实为 `None`，
不能用「0 成本」解释成改善。
