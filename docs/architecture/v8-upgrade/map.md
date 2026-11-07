# Wayfinder Map: V8 Architecture Upgrade

Status: active — P0 Decision Record **merged** (#869); P1 effect receipts **implemented** (04–06); D1–D7 decided 2026-11-07
Assessment: [assessment.md](assessment.md)
Intake: V8 architecture diagram (2026-11-07 Recommended Version) + existing UC Agent OS / governance / trust-eval / MetaInfer boundaries

本图把 V8「Agent · Decision · Inference · Training」闭环映射到 UltimateCoders 的**产品边界**与实现 backlog。决策票不是实现工作；实现票见 `tickets/`。

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
- [D6 Graph cutover ownership](issues/D6-graph-cutover-ownership.md): Graph-primary default-on is ready-for-human ops; not part of P0 code
- [D7 MetaInfer effect kind](issues/D7-metainfer-effect-kind.md): First non-git receipt = `metainfer-create` behind `UC_EFFECT_LEDGER_METAINFER`

## Implementation backlog

P0 (D1–D5) — Decision Record:

- Spec: [spec-p0.md](spec-p0.md)
- [01 decision record outbox + worker write](tickets/01-decision-record-outbox.md) — **implemented** (merged #869)
- [02 decision record dashboard read API](tickets/02-decision-record-dashboard.md) — **implemented** (merged #869)
- [03 docs architecture V8 mapping](tickets/03-docs-v8-mapping.md) — **implemented** (merged #869)

P1 (D7) — Tool / side-effect receipts:

- Spec: [spec-p1.md](spec-p1.md)
- [04 MetaInfer create EffectLedger](tickets/04-metainfer-effect-ledger.md) — **implemented**
- [05 push-on-release RuntimeState bind](tickets/05-push-ledger-bind.md) — **implemented**
- [06 docs P1 receipts](tickets/06-docs-p1-receipts.md) — **implemented**

P2 (later):

- Coding Champion/Challenger arena
- True replan (see agent-trust-eval ticket 18)
- Graph-primary cutover runbook (blocked by D6 human ops)

## Frontier (open, unblocked)

_(none — D1–D7 closed; P1 tickets 04–06 implemented)_

## Out of scope

- Building KV router / MoE / speculative decode inside UC
- SFT/RL/FSDP training OS
- Replacing TiKV/Qdrant with a new StateOS product name without behavioral change
- Rewriting Durable Runtime / Worker contract outside existing maps
