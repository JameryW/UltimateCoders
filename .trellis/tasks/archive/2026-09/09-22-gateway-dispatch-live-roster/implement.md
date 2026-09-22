# Execution plan

1. Write `crates/uc-grpc/tests/dispatch_live_roster.rs` per design.md.
2. `cargo test -p uc-grpc --test dispatch_live_roster` green.
3. Mutation check: temporarily stop sending `per_worker_topic` (or flip one
   expectation) → test red → restore → green. Records the test is not
   vacuous.
4. `cargo test -p uc-grpc --lib` green; `cargo fmt --check` clean.
5. Leave a reviewable diff; do not commit without authorization.

Risky files: none in production (test-only). Harness copy must track
`grpc_integration.rs` if that file changes.
