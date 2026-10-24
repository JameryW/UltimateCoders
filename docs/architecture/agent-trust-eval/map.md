# Wayfinder Map: Agent Trust / Eval

Status: active (versioned under docs/ — GitHub Issues publication pending explicit authorization)
Assessment: [agent-trust-eval-assessment.md](../agent-trust-eval-assessment.md) (surveyed 2026-10-24; D1–D8 decided 2026-10-24)
Intake: user gap list (评测 / 评审 / 防作弊 / 隔离 / 记忆 / 选择偏差) + P0–P2 priorities

本图在为实现票开票之前收敛「Agent 结果是否可信」路线上的决策。决策票**不是**实现工作，不得开 Trellis 任务。全部相关决策闭环后，再经 `$to-spec` / `$to-tickets` 开垂直切片实现票。

## Destination

一份可交接的规格 + 带显式 blocker 的实现票图：搜索/挑选/报告三套基准分离、正确性在 Agent 不可写环境重跑、完成条件由模型外代码判定（P0）；经排序一致性检验的近似评测、按失败类型的评审器（CV 阈值）、Hy-Memory 接入且旧事实标记取代（P1）；停滞 replan、强制网络隔离、项目本体 MCP、README 数字可复算 CI（P2）。新能力默认关在功能开关后。

## Notes

- Domain docs: `AGENTS.md`, `docs/agent-os-architecture.md`（尤其 F8）, `docs/inference-infra.md`, `docs/hy-memory.md`, `docs/dsec-sandbox.md`
- Skills: `$grilling`, `$domain-modeling`, then `$to-spec` / `$to-tickets` only after this map clears
- Standing preferences: feature flags off by default; approx↔full rank agreement re-tested when benches change; no CV thresholds without labeled data; Hy-Memory version-pinned + egress compliance
- Tracker mode: versioned docs tree until user authorizes GitHub map + `wayfinder:*` labels；授权后以 GitHub 为权威，本目录降为镜像

## Decisions so far

- [D1 three-benchmark split](issues/D1-three-benchmark-split.md): P0 两分（select vs hold-out report）；search 可暂共用但标 `eval_role`；对外 uplift 只用 report；P1 再严格三分
- [D2 independent correctness boundary](issues/D2-independent-correctness-boundary.md): 双闸——指纹 attestation + Agent 不可写环境独立重跑；Agent harness 不能单独关正确性门
- [D3 completion-conditions home](issues/D3-completion-conditions-home.md): digest 与 `completion_conditions` 分命名空间、从 contract 包导出；非 LLM 判定；不另立第二套完成权威
- [D4 approx-eval enablement gate](issues/D4-approx-eval-enablement-gate.md): 剪枝-only；Spearman/Kendall 在 report hold-out 过门后才开开关；uplift 禁引近似分；bench/code 变更复测失败则自动关
- [D5 failure-type reviewers](issues/D5-failure-type-reviewers.md): taxonomy 对齐 `routing.py`；一类一 reviewer + CV 阈值；无标注则该类型关闭；禁止默认 0.5「已校准」
- [D6 hy-memory wiring](issues/D6-hy-memory-wiring.md): 默认 MCP-only；`UC_HY_MEMORY_AUTO_CONTEXT=1` 才自动注入；supersede-not-delete 硬约束；钉版本、合规前关
- [D7 stall replan ownership](issues/D7-stall-replan-ownership.md): MVP 同契约换 Agent 重派一次后人工；真 replan 等 D3 完成条件落地
- [D8 sandbox isolation target](issues/D8-sandbox-isolation-target.md): 先让 `NetworkMode` None/Restricted/Full 成真（默认偏离线 + LLM allowlist）；Landlock FS 后置同地图票

## Frontier (open, unblocked)

_(none — decision tickets D1–D8 closed; fog items below are not decision tickets)_

## Blocked

_(none)_

## Not yet specified

- 项目本体（EvoOntology 风格）经 MCP 可查的最小词表与权威源
- README / 文档中的数字由哪条脚本复算、CI 失败策略（漂移告警 vs hard fail）
- 近似评测与 MetaInfer 内部 ranking 的边界（D4 已定 prune-only / uplift 禁引近似；实现票可再毕业细则）
- 标注数据管线（谁标、存在哪、如何防止泄漏进报告集）— D5 已定无标注则关闭；管线细节仍雾

## Out of scope

- 重做整个 Durable Runtime / Worker 契约（已有独立地图）
- 替换分层 Memory（TiKV/Qdrant）为 Hy-Memory
- 承诺生产 GPU 性能数字或外部论文复现
- 在无标注数据时假装已有交叉验证阈值
