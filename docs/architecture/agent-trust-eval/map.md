# Wayfinder Map: Agent Trust / Eval

Status: active — **P0–P2 MVP shipped**; P3 ready-for-agent (D9–D10; GitHub Issues publication still pending explicit authorization)
Assessment: [agent-trust-eval-assessment.md](../agent-trust-eval-assessment.md) (surveyed 2026-10-24; D1–D8 decided 2026-10-24; P2 MVP closed 2026-10-27; D9–D10 decided 2026-10-27; P3 ticketed 2026-10-27)
Intake: user gap list (评测 / 评审 / 防作弊 / 隔离 / 记忆 / 选择偏差) + P0–P2 priorities + P3 fog graduation

本图收敛「Agent 结果是否可信」路线上的决策，并挂接实现 backlog。决策票**不是**实现工作；P0 见 [spec-p0.md](spec-p0.md) + tickets/01–06（**implemented**）；P1 见 [spec-p1.md](spec-p1.md) + tickets/07–12（**implemented**）；P2 见 [spec-p2.md](spec-p2.md) + tickets/13–17（**implemented**）/ [18 deferred](tickets/18-true-replan-deferred.md)；P3 见 [spec-p3.md](spec-p3.md) + tickets/19–22。

## Destination

一份可交接的规格 + 带显式 blocker 的实现票图：搜索/挑选/报告三套基准分离、正确性在 Agent 不可写环境重跑、完成条件由模型外代码判定（P0）；经排序一致性检验的近似评测、按失败类型的评审器（CV 阈值）、Hy-Memory 接入且旧事实标记取代（P1）；停滞 replan、强制网络隔离、项目本体 MCP、README 数字可复算 CI（P2）。新能力默认关在功能开关后。

## Notes

- Domain docs: `AGENTS.md`, `docs/agent-os-architecture.md`（尤其 F8）, `docs/inference-infra.md`, `docs/hy-memory.md`, `docs/dsec-sandbox.md`
- Skills: `$grilling`, `$domain-modeling`, then `$to-spec` / `$to-tickets` only after this map clears
- Standing preferences: feature flags off by default; approx↔full rank agreement re-tested when benches change; no CV thresholds without labeled data; Hy-Memory version-pinned + egress compliance
- Tracker mode: versioned docs tree until user authorizes GitHub map + `wayfinder:*` labels；授权后以 GitHub 为权威，本目录降为镜像
- P0–P3 implementation backlogs are published below; decision tickets remain authority for D1–D10

## Implementation backlog

P0 (D1–D3) — **implemented** (docs-tree tickets; GitHub wayfinder publication still pending authorization):

- Spec: [spec-p0.md](spec-p0.md)
- [01 eval_role on results](tickets/01-eval-role-on-results.md) — **implemented** (`UC_EVAL_ROLE_ENFORCE`); was blocked by: none
- [02 report-only public uplift](tickets/02-report-only-public-uplift.md) — **implemented** (`UC_REPORT_UPLIFT_ENFORCE`); was blocked by 01
- [03 independent correctness re-eval](tickets/03-independent-correctness-reeval.md) — **implemented** (`UC_INDEPENDENT_REEVAL_ENFORCE`); was blocked by: none
- [04 dual-gate accept](tickets/04-dual-gate-accept.md) — **implemented** (`UC_DUAL_GATE_ENFORCE`); was blocked by 03
- [05 completion_conditions](tickets/05-completion-conditions.md) — **implemented** (`UC_COMPLETION_CONDITIONS_ENFORCE`); was blocked by: none
- [06 adapters and re-eval condition](tickets/06-adapters-and-reeval-condition.md) — **implemented** (`verify_command` / `step_condition` / `independent_reeval`); was blocked by: 05, 03

P1 (D4–D6) — **implemented** (docs-tree tickets; GitHub wayfinder publication still pending authorization):

- Spec: [spec-p1.md](spec-p1.md)
- [07 approx rank-agreement gate](tickets/07-approx-rank-agreement-gate.md) — **implemented** (agreement + arming latch; prune = 08)
- [08 approx prune and auto-disable](tickets/08-approx-prune-and-autodisable.md) — **implemented** (prune-only + uplift refuse + fingerprint auto-disarm); was blocked by 07
- [09 typed reviewers fail-closed](tickets/09-typed-reviewers-fail-closed.md) — **implemented** (`UC_TYPED_REVIEW_ENFORCE`; label-gated stub); was blocked by: none
- [10 reviewer CV thresholds](tickets/10-reviewer-cv-thresholds.md) — **implemented** (CV artifact loader; labels+threshold enablement; `typed_review_score`); was blocked by: 09
- [11 Hy-Memory supersede write](tickets/11-hy-memory-supersede-write.md) — **implemented** (supersede-not-delete on remember / MCP; no auto-inject); was blocked by: none
- [12 Hy-Memory auto context](tickets/12-hy-memory-auto-context.md) — **implemented** (`UC_HY_MEMORY_AUTO_CONTEXT` opt-in inject; soft skip; compliance: keep flag off until egress review); was blocked by: none

P2 (D7–D8) — **MVP complete except deferred** (13–17 implemented; 18 deferred):

- Spec: [spec-p2.md](spec-p2.md)
- [13 stall signal surface](tickets/13-stall-signal-surface.md) — **implemented** (`UC_STALL_REDISPATCH_ENFORCE` observe surface; swap is 14); was blocked by: none
- [14 same-contract agent redispatch](tickets/14-same-contract-agent-redispatch.md) — **implemented** (one-shot same-digest alternate agent; escalate on second stall; flag default off); was blocked by: 13
- [15 NetworkMode real enforcement](tickets/15-networkmode-real-enforcement.md) — **implemented** (`UC_NETWORK_MODE_ENFORCE`; child-only unshare none; Restricted fail-closed until T16; flag default off); was blocked by: none
- [16 default-offline allowlist](tickets/16-default-offline-allowlist.md) — **implemented** (`UC_NETWORK_DEFAULT_RESTRICTED` + `UC_NETWORK_ALLOWLIST`; `allowlist_preflight` Restricted applicator; flags default off; OS selective egress deferred); was blocked by: 15
- [17 Landlock FS later](tickets/17-landlock-fs-later.md) — **implemented** (`UC_LANDLOCK_ENFORCE`; child-only ctypes Landlock after NetworkMode wrap; fail closed; flag default off); was blocked by: 15 (preferably after 16)
- [18 true replan deferred](tickets/18-true-replan-deferred.md) — **deferred** (not P2 MVP)

P3 (D9–D10) — **in progress** (19+21 implemented; 20 in flight; 22 deferred):

- Spec: [spec-p3.md](spec-p3.md)
- [19 CONTEXT ontology loader](tickets/19-context-ontology-loader.md) — **implemented** (`UC_ONTOLOGY_MCP` loader + seed `CONTEXT.md`; soft-empty; no MCP tools); was blocked by: none
- [20 ontology MCP read-only](tickets/20-ontology-mcp-readonly.md) — Blocked by: none (was 19); PR in flight
- [21 docs numeric pin CI](tickets/21-docs-numeric-pin-ci.md) — **implemented** (`docs/numeric-pins.yaml` + hard-fail checker + `ci-docs-numeric-pins.yml`; SSE first; uplift ban); was blocked by: none
- [22 live numeric remeasure deferred](tickets/22-live-numeric-remeasure-deferred.md) — **deferred** (not P3 MVP)

Frontier for implementers: **20** (land/merge). Ticket **18** remains deferred. Ticket **22** remains deferred. Remaining fog below is not decision-ready.

## Decisions so far

- [D1 three-benchmark split](issues/D1-three-benchmark-split.md): P0 两分（select vs hold-out report）；search 可暂共用但标 `eval_role`；对外 uplift 只用 report；P1 再严格三分
- [D2 independent correctness boundary](issues/D2-independent-correctness-boundary.md): 双闸——指纹 attestation + Agent 不可写环境独立重跑；Agent harness 不能单独关正确性门
- [D3 completion-conditions home](issues/D3-completion-conditions-home.md): digest 与 `completion_conditions` 分命名空间、从 contract 包导出；非 LLM 判定；不另立第二套完成权威
- [D4 approx-eval enablement gate](issues/D4-approx-eval-enablement-gate.md): 剪枝-only；Spearman/Kendall 在 report hold-out 过门后才开开关；uplift 禁引近似分；bench/code 变更复测失败则自动关
- [D5 failure-type reviewers](issues/D5-failure-type-reviewers.md): taxonomy 对齐 `routing.py`；一类一 reviewer + CV 阈值；无标注则该类型关闭；禁止默认 0.5「已校准」
- [D6 hy-memory wiring](issues/D6-hy-memory-wiring.md): 默认 MCP-only；`UC_HY_MEMORY_AUTO_CONTEXT=1` 才自动注入；supersede-not-delete 硬约束；钉版本、合规前关
- [D7 stall replan ownership](issues/D7-stall-replan-ownership.md): MVP 同契约换 Agent 重派一次后人工；真 replan 等 D3 完成条件落地
- [D8 sandbox isolation target](issues/D8-sandbox-isolation-target.md): 先让 `NetworkMode` None/Restricted/Full 成真（默认偏离线 + LLM allowlist）；Landlock FS 后置同地图票
- [D9 project ontology MCP](issues/D9-project-ontology-mcp.md): 人维护 `CONTEXT.md` + 只读 MCP；默认关；写回/Evo 延后
- [D10 README numeric CI](issues/D10-readme-numeric-ci.md): 数字清单 + pin checker hard fail（先 SSE 族）；live 重测后置；对齐 D1/T2

## Frontier (open, unblocked)

_(none — decision tickets D1–D10 closed; remaining fog items below are not decision tickets)_

## Blocked

_(none)_

## Not yet specified

- 严格三分 search/select/report **数据集**拆分（D1 P1 follow-on；P1 approx 先用 report-role hold-out）
- 标注数据管线（谁标、存在哪、如何防止泄漏进报告集）— D5 已定无标注则关闭；管线细节仍雾

## Out of scope

- 重做整个 Durable Runtime / Worker 契约（已有独立地图）
- 替换分层 Memory（TiKV/Qdrant）为 Hy-Memory
- 承诺生产 GPU 性能数字或外部论文复现
- 在无标注数据时假装已有交叉验证阈值
