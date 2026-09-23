# CI clippy: cover all targets

## Goal

The exact hole that let test-code clippy warnings accumulate can never
reopen: CI lints test/bench/example targets, not just lib+bins.

## Background

4 baseline warnings fixed in `ab505c59`; 3 of 4 lived in `#[cfg(test)]`
code the CI gate never sees (evidence in `research/notes.md`).

## Requirements

1. One-line change in `.github/workflows/ci-rust.yml:79`:
   `cargo clippy --workspace -- -D warnings` →
   `cargo clippy --workspace --all-targets -- -D warnings`.
   Same job, same features (default), same deny flag. No trigger-path
   change needed (`crates/**` already covered).
2. No guard script: clippy ownership stays in CI (no precedent for
   cargo-shelling guards; local repro is one command).
3. No production code changes.

## Acceptance

- `cargo clippy --workspace --all-targets` locally clean (already
  verified: zero warnings post-`ab505c59`).
- YAML edit is a single line; `git diff --stat` shows only
  `.github/workflows/ci-rust.yml`.
- Cannot run the CI job itself locally; do not claim CI green — the
  claim is "same command CI will run is green on this tree".

## Out of scope

- `--all-features` matrix, toolchain pinning, fixing any non-clippy CI
  gaps, backfilling a guard script.

## Decisions

- Scope fixed by evidence (CI one-liner; no guard script). No user-owned
  product decision exists in this task.

## Open questions

None.
