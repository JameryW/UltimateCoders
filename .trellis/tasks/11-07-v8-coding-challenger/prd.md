# PRD — V8 P2 coding Champion/Challenger

## Background

P0 Decision Record (#869) and P1 EffectLedger receipts (#870) merged. Next Evidence OS slice: coding-domain Champion/Challenger per D8 — same-contract one-shot challenger after success, report-role compare, no auto-promote.

Tickets: 07–09 under `docs/architecture/v8-upgrade/`. Spec: `spec-p2.md`.

## Requirements

1. `coding_challenger.py` policy module (flag, episode, decide/apply).
2. Orchestrator success-path wire.
3. Docs/map updates.

## Out of scope

Auto-promote, MetaInfer Oracle arena, true replan, graph cutover.

## Done when

spec-p2 AC green; focused pytest pass.
