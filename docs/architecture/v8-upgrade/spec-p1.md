# Spec P1 — Tool / side-effect receipts (EffectLedger beyond git)

Blocked by: D7 (decided). Builds on EffectLedger used by MergeArbiter / WorkspaceManager push.

## Goal

Close the V8 Agent OS gap where only git publish has durable side-effect receipts: add **MetaInfer create** as the first non-git EffectLedger kind, keep `remote_jobs` as the operational store, and ensure coding-path branch pushes can bind a ledger.

## What to build

1. **Flag** `UC_EFFECT_LEDGER_METAINFER` (default off / unset = off).
2. **MetaInferAdapter** (when flag on and `self.state` present): around the create `POST …/tasks` —
   - `stable_effect_id("metainfer-create", backend_id, operation_id, request_sha256)`
   - `prepare` → if `CONFIRMED` and `remote_jobs` already has `remote_id`/`result`, do not POST again
   - `mark_in_flight` immediately before POST
   - on valid `remote_id`: `confirm` with `{remote_id}`
   - on ambiguous failure (existing `RemoteStateUncertainError` path): `mark_unknown`
   - reconcile: if `remote_jobs[operation_id].remote_id` or `.result` present → `confirm`; else leave UNKNOWN (do not invent new HTTP beyond existing recovery)
3. **Worker / WorkspaceManager**: when a workspace manager will push on release (`push_on_release`), bind RuntimeState before release so `push-branch` receipts are not silently skipped on the coding path.
4. **Docs**: update V8 map + agent-os gap note; optional short note in `docs/inference-infra.md`.

## Acceptance criteria

- [x] Flag off: MetaInfer behavior byte-identical for ledger writes (no `tool_effects` rows).
- [x] Flag on + success create → EffectLedger `CONFIRMED` with `kind=metainfer-create`.
- [x] Flag on + transport/uncertain after claim → `UNKNOWN` (not `FAILED`).
- [x] Flag on + prepare finds `CONFIRMED` → no second POST (covered by test with fake client / stub).
- [x] Existing push-effect and MetaInfer reliability tests stay green.
- [x] Coding-path: when `push_on_release` and RuntimeState available, `_push_subtask_branch` uses the ledger (bind happens).
- [x] Reconcile is crash-detectable too: `begin_reconcile` takes the ledger's soft lease, a live lease refuses a second reconciler, and an expired (or legacy, lease-less) one is taken over instead of leaving the effect wedged in `RECONCILING` (T37).

## Out of scope

- Replacing `remote_jobs` with EffectLedger
- MCP `write_file` receipts
- Kill/control path ledger wrapping
- Extracting a shared push runner (nice-to-have; not required if duplication stays)
