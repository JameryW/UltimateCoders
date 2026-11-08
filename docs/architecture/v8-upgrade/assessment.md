# V8 Architecture Upgrade — Assessment

Surveyed: 2026-11-07 (Asia/Shanghai). Source diagram: *Distributed Agent · Decision · Inference · Training Service Technical Architecture V8 (2026-11-07 Recommended Version)*.

## Verdict

UltimateCoders already is a strong **Agent OS + Governance/Policy + Evidence-gated coding control plane**, with inference intentionally externalized via MetaInfer. Treat V8 as a **checklist for hardening the Agent + Decision + Evidence plane**, not as a mandate to grow an in-tree GPU inference or Learning/FSDP stack.

**MVP closeout (2026-11-07, D11):** checklist slices P0–P4 landed and merged (#869–#873). True replan execute (T18), Learning OS (D3), and graph-primary default-on remain deferred or human-gated as before — outside this wave.

**Follow-on (2026-11-08, D14 / T18d):** replan-execute readiness runbook + dry-run probe landed (same ops class as P4 graph cutover). Precursor evidence only — does **not** satisfy true replan execute (ticket 18).

## Domain coverage (compact)

| V8 domain | UC maturity | Notes |
|---|---|---|
| 1 Agent OS | High code / medium default-on | DAG lease/fence/commit-once exist behind `UC_GRAPH_SHADOW`; MergeArbiter + EffectLedger for git; general tool receipts still thin |
| 2 Decision + Context + StateOS | High policy/context; medium StateOS naming | Governance Policy Kernel + Context Compiler exist; **no unified Decision Record**; Capability Tokens stay Grant/Mandate |
| 3 Inference Runtime | Intentional external | MetaInfer boundary; KV ephemeral invariant already documented |
| 4 Learning OS | Absent | Out of MVP; trajectory export only if a later decision re-opens scope |
| 5 Research / Evidence OS | Medium–high | agent-trust-eval P0–P3 shipped; Champion/Challenger unnamed for coding agents |
| Horizontal layers | High control/memory/infra | Control plane already out of token hot path |

## V8 constraints vs UC posture

1. **Control plane out of token hot path** — aligned (Gateway schedules; workers/MetaInfer call models).
2. **KV discardable / recomputable** — aligned (`docs/agent-os-architecture.md` invariant #6).
3. **Prod inference physically isolated from RL** — aligned by non-participation in Learning OS + MetaInfer externalization.
4. **EP / KV / side-effect boundaries** — partially aligned (git EffectLedger; broad tool receipts still open).

## Highest-leverage upgrade slices (no GPU engine)

1. **Unify Decision Record** (Governance × Routing × Context × terminal verdict) — this map's P0.
2. **Generalize Effect Ledger → tool/side-effect receipts**.
3. **Coding-domain Champion/Challenger** reusing trust/eval gates.
4. **Graph-primary durable runtime cutover** (ops/human gate; code largely exists).
5. **True replan** (agent-trust-eval ticket 18 deferred).

## Explicitly out of MVP

- In-tree KV-aware router / MoE / prefill-decode engine
- SFT / Reasoning RL / Agentic RL / 5D+FSDP Learning OS
- Model-weight registry & canary release train
- Flipping `UC_GRAPH_SHADOW` default without an ops cutover decision
