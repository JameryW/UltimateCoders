# MetaInfer architecture reliability and operations

## Goal and authorization

Complete the eight findings in [review.md](review.md), approved by the user's
"修复所有问题" response to the architecture review. This continues that approved
plan; the previous reliability and CI tasks are completed. The unrelated
session-fallback task is not owned by this context.

Baseline: `2b22e9adadb3def4f3ffb34e7f6afa8075524f48`.
Tracker: GitHub discovery is authenticated; this user-approved repair brief is
the source of scope. No new remote issue publication has been requested.

## Requirements and acceptance

The severity, evidence, source anchors and detailed acceptance criteria for R1
through R8 are authoritative in review.md. All eight are in scope:

1. R1: projection writes cannot reverse Gateway controls or reset attempts;
   explicit retry creates a fresh attempt and survives recovery.
2. R2: cancellation requires all-writer quiescence evidence. An acknowledgement
   or legacy finished flag alone retains quarantine, workspace and capacity.
3. R3: shared capacity uses a durable, cancellable, fair resource wait; aliases
   can share a stable backend identity. Waiting does not spend execution retries.
4. R4: version-checked recovery and audit, plus experiment/resource/quarantine
   visibility in the Dashboard. Never infer remote death from a stale heartbeat.
5. R5: bounded indexed pending queries, delivery claims and connection reuse;
   retention keeps replay-safe outcome tombstones. SQLite/PostgreSQL agree.
6. R6: pre-launch workspace sharing proof and content-addressed artifact
   publication/download across hosts, with integrity and delivery visibility.
7. R7: actual device/runtime/model identity and whole-workflow device reservation;
   record measurement conditions and detect environment changes.
8. R8: pinned real MetaInfer/GPU integration gate and reproducible evidence for
   acceptance, rejection, cancellation, recovery and artifact delivery.

## Validation

- Regression tests exercise public controls, real SQLite transactions, HTTP
  transport boundaries, benchmark/workspace behavior and API/UI contracts.
- Run relevant Rust, Python, TypeScript and Dashboard checks, then independent
  Spec/Standards review and Trellis quality verification.
- Preserve deployment compatibility through additive contracts and explicit
  capability negotiation. Document rollout, isolation and recovery.
- Record actual GPU integration execution separately from simulated tests.

## Open external input

Real MetaInfer service address/configuration was requested asynchronously. The
local host has an RTX 4060 Laptop GPU (8 GiB), driver 617.14. Ollama is not the
specialized MetaInfer backend. Code and fault validation continue while this
input is pending; R8 may not be claimed verified without real evidence.

## Out of scope

Forking upstream MetaInfer into UC, automatically clearing uncertain writers,
weakening Oracle thresholds, and enabling graph shadow without migration.
