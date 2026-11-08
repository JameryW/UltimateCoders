# V8 is an Agent + Decision + Evidence checklist

UltimateCoders adopts the 2026-11-07 V8 closed-loop diagram as a **hardening checklist** for the Agent OS, Decision/Context/StateOS, and Research/Evidence planes it already owns. Inference Runtime stays external (MetaInfer); Learning OS is out of scope until a later product decision. Building an in-tree GPU serving or training stack would reverse the MetaInfer acceptance boundary and put the control plane on a path V8 itself forbids (token hot path / prod⊥RL isolation).

Status: accepted

## Considered options

1. Full V8 product expansion (Agent + Inference + Learning + Research in one deployable)
2. **Checklist alignment** (chosen) — close Decision Record / receipts / evidence gaps; keep MetaInfer and defer Learning
3. Docs-only mapping with no code — rejected because Decision Record is a missing audit join that already has ready seams

## Consequences

- Wayfinder lives at `docs/architecture/v8-upgrade/`
- P0 Decision Record is implemented (`decision_record` RuntimeState namespace, Worker start/terminal hooks, `GET /dashboard/api/decisions`); graph default-on and Learning remain human-gated
- Checklist P0–P4 shipped and merged (#869–#873); map status **complete (MVP)** per [D11](../architecture/v8-upgrade/issues/D11-v8-mvp-closure.md)
- T18a–d precursors shipped and merged (#872/#875/#876/#877); precursor wave closed per [D15](../architecture/v8-upgrade/issues/D15-t18-precursor-closeout.md); map status **complete (MVP + T18 precursors)**. True replan execute stays on trust-eval ticket 18 (deferred)
