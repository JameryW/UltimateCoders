# V8 Architecture Upgrade — Assessment

Surveyed: 2026-11-07 (Asia/Shanghai). Source diagram: *Distributed Agent · Decision · Inference · Training Service Technical Architecture V8 (2026-11-07 Recommended Version)*.

## Verdict

UltimateCoders already is a strong **Agent OS + Governance/Policy + Evidence-gated coding control plane**, with inference intentionally externalized via MetaInfer. Treat V8 as a **checklist for hardening the Agent + Decision + Evidence plane**, not as a mandate to grow an in-tree GPU inference or Learning/FSDP stack.

**MVP closeout (2026-11-07, D11):** checklist slices P0–P4 landed and merged (#869–#873). True replan execute (T18), Learning OS (D3), and graph-primary default-on remain deferred or human-gated as before — outside this wave.

**Follow-on (2026-11-08, D14 / T18d):** replan-execute readiness runbook + dry-run probe landed (same ops class as P4 graph cutover). Precursor evidence only — does **not** satisfy true replan execute (ticket 18).

**T18 precursor closeout (2026-11-08, D15):** T18a–d landed and merged (#872/#875/#876/#877). Precursor ladder closed — no further silent precursor slices under this map without a new decision. True replan execute remains deferred on trust-eval ticket 18; Learning OS (D3) and graph-primary default-on remain deferred / human-gated.

**P6 control receipts (2026-11-08, D16):** the MetaInfer kill/control POST now records `kind=metainfer-kill` behind `UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off). That closes the last external side effect with the ledger's lost-response shape; local `write_file` receipts are an evidence-surface decision, not a ledger kind.

**P7 local tool receipts (2026-11-08, D17):** local effects now land as an attempt-scoped `terminal.workspace` block on the Decision Record (changed paths + tool names, never content), derived from the git diff the sandbox already computes. No per-tool interception and no new store.

**P8 preserve-effects sourcing (2026-11-08, D18):** the worker now supplies `confirmed_effect_ids` on a governed stall, derived from CONFIRMED `tool_effects` records (subtask branch / MetaInfer experiment prefix), so the gateway's `Replan.preserve_effects` carries real receipts instead of an always-empty list. Effects that cannot be scoped stay empty — fewer protections, never a pretended one.

**P9 Decision Record trail (2026-11-08, D19):** the dashboard's task detail now renders the attempt trail (verdict, route, `ctx <12 hex>` pointer, P7 local effects) from the existing decisions response — a pure read surface, with ordering and counts left to the endpoint.

**P10 trail paging (2026-11-08, D20):** the decisions route gained the context-manifest route's `order=recent` + `before` cursor contract, so a long trail is fully reachable; key order stays a cursorless window and the panel pages deduped, keeping rows when a page fails.

**P11 subtask scope (2026-11-09, D21):** the trail scopes to one subtask via the row's subtask id and pages inside that scope — the control D19 deferred until paging existed, now at parity with the context-compile trail.

## Domain coverage (compact)

| V8 domain | UC maturity | Notes |
|---|---|---|
| 1 Agent OS | High code / medium default-on | DAG lease/fence/commit-once exist behind `UC_GRAPH_SHADOW`; MergeArbiter + EffectLedger for git, MetaInfer create and kill/control; local tool receipts land as attempt-scoped workspace evidence (P7) |
| 2 Decision + Context + StateOS | High policy/context; medium StateOS naming | Governance Policy Kernel + Context Compiler exist; unified Decision Record landed (P0) and now carries workspace evidence (P7); Capability Tokens stay Grant/Mandate |
| 3 Inference Runtime | Intentional external | MetaInfer boundary; KV ephemeral invariant already documented |
| 4 Learning OS | Absent | Out of MVP; trajectory export only if a later decision re-opens scope |
| 5 Research / Evidence OS | Medium–high | agent-trust-eval P0–P3 shipped; Champion/Challenger unnamed for coding agents |
| Horizontal layers | High control/memory/infra | Control plane already out of token hot path |

## V8 constraints vs UC posture

1. **Control plane out of token hot path** — aligned (Gateway schedules; workers/MetaInfer call models).
2. **KV discardable / recomputable** — aligned (`docs/agent-os-architecture.md` invariant #6).
3. **Prod inference physically isolated from RL** — aligned by non-participation in Learning OS + MetaInfer externalization.
4. **EP / KV / side-effect boundaries** — partially aligned (git EffectLedger; MetaInfer create/control receipts; local tool receipts on the Decision Record; confirmed effects now feed the watchdog).

## Highest-leverage upgrade slices (no GPU engine)

1. **Unify Decision Record** (Governance × Routing × Context × terminal verdict) — this map's P0.
2. **Generalize Effect Ledger → tool/side-effect receipts** (P1 `metainfer-create`, P6 `metainfer-kill`; local effects land as Decision Record workspace evidence, P7).
3. **Coding-domain Champion/Challenger** reusing trust/eval gates.
4. **Graph-primary durable runtime cutover** (ops/human gate; code largely exists).
5. **True replan** (agent-trust-eval ticket 18 deferred).

## Explicitly out of MVP

- In-tree KV-aware router / MoE / prefill-decode engine
- SFT / Reasoning RL / Agentic RL / 5D+FSDP Learning OS
- Model-weight registry & canary release train
- Flipping `UC_GRAPH_SHADOW` default without an ops cutover decision
