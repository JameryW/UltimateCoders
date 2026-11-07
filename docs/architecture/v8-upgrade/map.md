# Wayfinder Map: V8 Architecture Upgrade

Status: **complete (MVP)** — P0–P4 **merged** (#869–#873); closeout #874; T18b checklist **merged** (#875); T18c accounting **implemented** (`UC_REPLAN_ACCOUNTING_ENFORCE`); D1–D13 decided 2026-11-07; full T18 execute still deferred
Assessment: [assessment.md](assessment.md)
Intake: V8 architecture diagram (2026-11-07 Recommended Version) + existing UC Agent OS / governance / trust-eval / MetaInfer boundaries

本图把 V8「Agent · Decision · Inference · Training」闭环映射到 UltimateCoders 的**产品边界**与实现 backlog。决策票不是实现工作；实现票见 `tickets/`。MVP 波次已关闭（D11）；勿在本图下静默抓取 T18 execute。

## Destination

一份可交接的升级规格：明确 UC **拥有**的域（Agent OS、Decision/StateOS、Evidence）、**外部化**的域（Inference Runtime）、**本期不做**的域（Learning OS），并交付带显式 blocker 的垂直切片。新能力默认不破坏既有默认路径；审计落库与 Context Manifest 一样 best-effort、不含 prompt 内容。

## Notes

- Domain docs: `AGENTS.md`, `CONTEXT.md`, `docs/architecture.md`, `docs/agent-os-architecture.md`, `docs/governance/`, `docs/inference-infra.md`, `docs/architecture/agent-trust-eval/map.md`
- ADR: [docs/adr/0001-v8-as-agent-decision-evidence-checklist.md](../../adr/0001-v8-as-agent-decision-evidence-checklist.md)
- Tracker mode: versioned docs tree until user authorizes GitHub map + labels；授权后以 GitHub 为权威，本目录降为镜像
- Standing preferences: no GPU engine in UC; feature flags for behavioral gates; audit trails always log-safe

## Decisions so far

- [D1 V8 scope posture](issues/D1-v8-scope-posture.md): V8 = Agent+Decision+Evidence hardening checklist; not a GPU/Learning product expansion
- [D2 Inference boundary](issues/D2-inference-boundary.md): Inference Runtime stays MetaInfer-external; UC owns acceptance/worktree/cancellation only
- [D3 Learning OS deferral](issues/D3-learning-os-deferral.md): Learning OS out of MVP
- [D4 Capability token model](issues/D4-capability-token-model.md): Keep Grant/Mandate; no portable opaque capability tokens in MVP
- [D5 Decision Record shape](issues/D5-decision-record-shape.md): One attempt-scoped Decision Record aggregating route + context digest + terminal verdict
- [D6 Graph cutover ownership](issues/D6-graph-cutover-ownership.md): Graph-primary default-on is ready-for-human ops; not part of P0–P3 code
- [D7 MetaInfer effect kind](issues/D7-metainfer-effect-kind.md): First non-git receipt = `metainfer-create` behind `UC_EFFECT_LEDGER_METAINFER`
- [D8 Coding Champion/Challenger](issues/D8-coding-challenger.md): Same-contract one-shot report-role challenger after success; no auto-promote
- [D9 Replan proposal only](issues/D9-replan-proposal-only.md): T18a — structured `replan` proposal on watchdog verdict; no DAG execute
- [D10 Graph cutover runbook scope](issues/D10-graph-cutover-runbook.md): P4 = ops runbook + dry-run readiness; no `UC_GRAPH_SHADOW` default flip
- [D11 V8 MVP closure](issues/D11-v8-mvp-closure.md): Close V8 MVP wave after P0–P4; T18 execute / Learning OS / graph default-on stay deferred or human-gated
- [D12 Replan execute checklist](issues/D12-replan-execute-checklist.md): T18b — approve `replan` → Attention checklist only; no DAG/digest mutation
- [D13 Durable replan accounting](issues/D13-replan-accounting.md): T18c — `WorkCase.replans_used` behind `UC_REPLAN_ACCOUNTING_ENFORCE`; no DAG/digest

## Implementation backlog

P0 (D1–D5) — Decision Record: **merged** (#869)

P1 (D7) — Tool / side-effect receipts: **merged** (#870)

P2 (D8) — Coding Champion/Challenger: **merged** (#871)

P3 (D9) — Replan proposal only (T18a): **merged** (#872)

- Spec: [spec-p3.md](spec-p3.md)
- [10 ProposalKind replan lockstep](tickets/10-replan-kind-lockstep.md) — **implemented**; was blocked by: none
- [11 evaluator + orchestrator wire](tickets/11-replan-proposal-evaluate-wire.md) — **implemented** (`UC_REPLAN_PROPOSAL_ENFORCE` default off); was blocked by: 10
- [12 docs P3](tickets/12-docs-p3-replan-proposal.md) — **implemented**; was blocked by: none

P4 (D6 / D10) — Graph-primary cutover runbook: **merged** (#873)

- Spec: [spec-p4.md](spec-p4.md)
- Runbook: [graph-cutover-runbook.md](../graph-cutover-runbook.md)
- Dry-run: `scripts/check-graph-cutover-readiness.py`
- [13 ops runbook](tickets/13-graph-cutover-runbook.md) — **implemented**; was blocked by: D6/D10
- [14 dry-run readiness script](tickets/14-graph-cutover-dry-run.md) — **implemented**; was blocked by: 13
- [15 docs P4](tickets/15-docs-p4-graph-cutover.md) — **implemented**; was blocked by: none

P5 (D11) — V8 MVP closeout (docs): **implemented**

- Spec: [spec-p5.md](spec-p5.md)
- [16 docs V8 MVP closeout](tickets/16-docs-v8-mvp-closeout.md) — **implemented**; was blocked by: D11

Follow-on (D12 — checklist): **merged** (#875)

- Spec: [spec-t18b.md](spec-t18b.md)
- [17 replan execute checklist wire](tickets/17-replan-execute-checklist-wire.md) — **implemented** (`UC_REPLAN_EXECUTE_CHECKLIST_ENFORCE` default off); was blocked by: D12
- [18 docs T18b](tickets/18-docs-t18b-checklist.md) — **implemented**; was blocked by: none

Follow-on (D13 — accounting): **implemented**

- Spec: [spec-t18c.md](spec-t18c.md)
- [19 durable replan accounting wire](tickets/19-replan-accounting-wire.md) — **implemented** (`UC_REPLAN_ACCOUNTING_ENFORCE` default off); was blocked by: D13
- [20 docs T18c](tickets/20-docs-t18c-accounting.md) — **implemented**; was blocked by: none

Later (still deferred — not grabable as full execute):

- True replan **execute** ([agent-trust-eval ticket 18](../agent-trust-eval/tickets/18-true-replan-deferred.md) remainder — T18a/T18b/T18c do not satisfy)
- Learning OS (D3)
- Graph-primary **default-on** (human/ops gate per D6/D10; Compose defaults stay off)

## Frontier (open, unblocked)

- _(none for V8 follow-on accounting; true T18 execute remains deferred)_

## Out of scope

- Building KV router / MoE / speculative decode inside UC
- SFT/RL/FSDP training OS
- Replacing TiKV/Qdrant with a new StateOS product name without behavioral change
- Rewriting Durable Runtime / Worker contract outside existing maps
