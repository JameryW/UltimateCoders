# PRD — V8 P1 MetaInfer + push EffectLedger receipts

## Background

P0 Decision Record merged (#869). Next V8 checklist item: generalize side-effect receipts beyond git. D7 chose MetaInfer create POST as the first non-git kind, flag-gated, keeping `remote_jobs` as operational store.

Tickets: `docs/architecture/v8-upgrade/tickets/04|05|06-*.md`, spec `spec-p1.md`.

## Requirements

1. `UC_EFFECT_LEDGER_METAINFER` (default off) wraps MetaInfer create with EffectLedger (`metainfer-create`).
2. Lazy-bind RuntimeState for `push_on_release` so coding-path branch pushes get ledger receipts.
3. Docs/map updates for P1.

## Out of scope

Replace remote_jobs; MCP file writes; kill path; shared push-runner extract (optional).

## Done when

spec-p1 AC checked; focused pytest green including new `test_metainfer_effects.py`.
