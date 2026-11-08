# Wayfinder Map: V8 Architecture Upgrade

Status: **complete (MVP + T18 precursors + P6/P7 receipts + P8 effect sourcing + P9/P10/P11 decision trail)** — P0–P4 **merged** (#869–#873); closeout #874; T18a–d **merged** (#872/#875/#876/#877); precursor closeout **complete** (D15 / ticket 23); P6 MetaInfer kill/control receipts **merged** (#880); P7 local tool receipts **merged** (#881); P8 watchdog effect sourcing **merged**; P9 Decision Record dashboard trail **merged** (#882); P10 trail paging **merged** (#883); P11 subtask scope **implemented** (D21); D1–D21 decided; full T18 execute still deferred
Assessment: [assessment.md](assessment.md)
Intake: V8 architecture diagram (2026-11-07 Recommended Version) + existing UC Agent OS / governance / trust-eval / MetaInfer boundaries

本图把 V8「Agent · Decision · Inference · Training」闭环映射到 UltimateCoders 的**产品边界**与实现 backlog。决策票不是实现工作；实现票见 `tickets/`。MVP 波次已关闭（D11）；T18 前驱波次已关闭（D15）；勿在本图下静默抓取 T18 execute 或发明新前驱切片。

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
- [D14 Replan execute readiness](issues/D14-replan-execute-readiness.md): T18d — ops runbook + dry-run; no execute gate / no DAG/digest / no `admit_execution`
- [D15 T18 precursor closeout](issues/D15-t18-precursor-closeout.md): Close T18a–d ladder; no silent further precursors or execute without a new decision
- [D16 MetaInfer control receipt](issues/D16-metainfer-control-receipt.md): Next side-effect receipt = `metainfer-kill` behind `UC_EFFECT_LEDGER_METAINFER_CONTROL`; local `write_file` receipts stay out of the ledger (evidence surface, own decision)
- [D17 Local tool receipts](issues/D17-local-tool-receipts.md): Local effects land as attempt-scoped `workspace` evidence on the Decision Record (git diff + tool names), never per-tool interception
- [D18 Preserve-effects sourcing](issues/D18-preserve-effects-sourcing.md): The worker derives `confirmed_effect_ids` from CONFIRMED `tool_effects` records (subtask branch / experiment prefix); unscopable effects stay empty
- [D19 Decision Record dashboard trail](issues/D19-decision-record-dashboard-trail.md): The attempt record gets a read surface (verdict / route / context pointer / local effects); ordering and counts belong to the endpoint, no new API
- [D20 Decision trail paging](issues/D20-decisions-trail-paging.md): The decisions route gains the manifest route's `order=recent` + `before` cursor contract, so a long trail is reachable; key order stays a cursorless window
- [D21 Decision trail subtask scope](issues/D21-decisions-trail-subtask-scope.md): Rows' subtask ids scope the trail and paging stays inside the scope; no new endpoint/parameter, no verdict/route filtering

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

Follow-on (D13 — accounting): **merged** (#876)

- Spec: [spec-t18c.md](spec-t18c.md)
- [19 durable replan accounting wire](tickets/19-replan-accounting-wire.md) — **implemented** (`UC_REPLAN_ACCOUNTING_ENFORCE` default off); was blocked by: D13
- [20 docs T18c](tickets/20-docs-t18c-accounting.md) — **implemented**; was blocked by: none

Follow-on (D14 — readiness): **merged** (#877)

- Spec: [spec-t18d.md](spec-t18d.md)
- Runbook: [replan-execute-readiness-runbook.md](../replan-execute-readiness-runbook.md)
- Dry-run: `scripts/check-replan-execute-readiness.py`
- [21 replan execute readiness wire](tickets/21-replan-execute-readiness-wire.md) — **implemented**; was blocked by: D14
- [22 docs T18d](tickets/22-docs-t18d-readiness.md) — **implemented**; was blocked by: none

Follow-on (D15 — precursor closeout): **implemented** (complete)

- Spec: [spec-t18-closeout.md](spec-t18-closeout.md)
- [23 docs T18 precursor closeout](tickets/23-docs-t18-precursor-closeout.md) — **implemented**; was blocked by: D15

P6 (D16) — MetaInfer kill/control receipts: **merged** (#880)

- Spec: [spec-p6.md](spec-p6.md)
- [24 MetaInfer kill/control receipt](tickets/24-metainfer-kill-ledger.md) — **implemented** (`UC_EFFECT_LEDGER_METAINFER_CONTROL` default off); was blocked by: D16
- [25 docs P6](tickets/25-docs-p6-control-receipts.md) — **implemented**; was blocked by: none

P7 (D17) — Local tool receipts (Decision Record evidence): **merged** (#881)

- Spec: [spec-p7.md](spec-p7.md)
- [26 local tool receipts](tickets/26-local-tool-receipts.md) — **implemented** (attempt-scoped `terminal.workspace`; no flag, audit-only); was blocked by: D17
- [27 docs P7](tickets/27-docs-p7-local-tool-receipts.md) — **implemented**; was blocked by: none

P8 (D18) — Confirmed effects for the watchdog: **implemented**

- Spec: [spec-p8.md](spec-p8.md)
- [28 confirmed effects watchdog](tickets/28-confirmed-effects-watchdog.md) — **implemented** (worker supplies `confirmed_effect_ids` from `tool_effects`; unscopable stays empty); was blocked by: D18
- [29 docs P8](tickets/29-docs-p8-confirmed-effects.md) — **implemented**; was blocked by: none

P9 (D19) — Decision Record dashboard trail: **merged** (#882)

- Spec: [spec-p9.md](spec-p9.md)
- [30 decision record dashboard trail](tickets/30-decision-record-dashboard-trail.md) — **implemented** (read-only render of the existing decisions response; no API change); was blocked by: D19
- [31 docs P9](tickets/31-docs-p9-decision-trail.md) — **implemented**; was blocked by: none

P10 (D20) — Decision trail paging: **merged** (#883)

- Spec: [spec-p10.md](spec-p10.md)
- [32 decisions trail paging](tickets/32-decisions-trail-paging.md) — **implemented** (`order=recent` + `before` cursor, same contract as the manifest route); was blocked by: D20
- [33 docs P10](tickets/33-docs-p10-paging.md) — **implemented**; was blocked by: none

P11 (D21) — Decision trail subtask scope: **implemented**

- Spec: [spec-p11.md](spec-p11.md)
- [34 decisions trail subtask scope](tickets/34-decisions-trail-subtask-scope.md) — **implemented** (row subtask ids scope the trail; paging stays inside the scope); was blocked by: D21
- [35 docs P11](tickets/35-docs-p11-scope.md) — **implemented**; was blocked by: none

Later (still deferred — not grabable as full execute):

- True replan **execute** ([agent-trust-eval ticket 18](../agent-trust-eval/tickets/18-true-replan-deferred.md) remainder — T18a–d precursors do not satisfy; blocked until product-stable `UC_COMPLETION_CONDITIONS_ENFORCE` and trusted contract-version ops)
- Learning OS (D3)
- Graph-primary **default-on** (human/ops gate per D6/D10; Compose defaults stay off)

## Frontier (open, unblocked)

_(empty — no grabable implement tickets; P6 closed under D16, P7 under D17, P8 under D18, P9 under D19, P10 under D20 and P11 under D21; true replan execute remains deferred on trust-eval ticket 18; Learning OS and graph default-on stay deferred / human-gated. Local `write_file` receipts are evidence on the Decision Record, not ledger kinds, and are already closed by D17; the watchdog's `preserve_effects` source is closed by D18; the Decision Record read surface, its paging and its scope are closed by D19/D20/D21 — do not reopen any of them here without a new decision. Do not invent T18e+ precursors or silent execute under this map without a new decision.)_

## Out of scope

- Building KV router / MoE / speculative decode inside UC
- SFT/RL/FSDP training OS
- Replacing TiKV/Qdrant with a new StateOS product name without behavioral change
- Rewriting Durable Runtime / Worker contract outside existing maps
