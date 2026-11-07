# D2 — Inference boundary

Status: decided
Decided: 2026-11-07

## Question

Should UC implement V8 Inference Runtime (KV-aware router, MoE placement, prefill/decode) in-tree?

## Decision

**No.** Inference Runtime remains an external MetaInfer (or compatible) service. UC keeps worktree leases, operation identity, cancellation, Oracle acceptance, and artifact evidence.

## Why

Already documented in `docs/inference-infra.md` and `docs/architecture.md`. V8 itself requires the control plane out of the token hot path and physical isolation of prod inference from RL — externalization is the conforming choice.
