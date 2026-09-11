# Wayfinder Map: Durable Agent Runtime P0

Status: active
GitHub: map #632 · D4 #633 · D5 #634 · D6 #635 · D7 #636 (created 2026-09-11; GitHub mode is authoritative, these files are mirrors)
Assessment: docs/architecture/durable-runtime-migration-assessment.md (approved 2026-09-11)
Decided already: D1 state-table authority + audit events · D2 one-shot authority flip to Rust · D3 one-shot new worker contract (lockstep, handshake-gated)

This map resolves the remaining route decisions. Decision tickets are NOT implementation work and must not start a Trellis task. When all four are closed, implementation tickets T1–T7 (see assessment §5) are filed against the tracker.

## Decision tickets

- D4 nats-transport-convergence — DECIDED (#633): JetStream-only hard dep; effect_class-gated LocalExecutor; P0 converges dispatch plane only
- D5 commit-barrier-ownership — MergeArbiter stays Python with Rust-issued fenced barrier vs rewritten in Rust (gates P1-2, needed before T-plan review)
- D6 resume-semantics — DECIDED (#635): recompute-ready from committed nodes (no snapshots, wave checkpoints deleted); soft pause + UC_PAUSE_GRACE_SECS escalation; cancel-attempt-keep-node into T7
- D7 upgrade-window-inflight-policy — DECIDED (#636): auto-resume after one-shot import (uncommitted attempts re-READY via timeout+fence); stale-envelope messages term-dropped with counter+alert; release-notes checklist fixed

## Out of scope (per assessment §4 / plan §21)

ExecutionScope design, Context Compiler, sandbox env allowlist detail, PlacementScore, Execution Optimizer, Blackboard review, market scheduling — P1+ maps after P0 acceptance.
