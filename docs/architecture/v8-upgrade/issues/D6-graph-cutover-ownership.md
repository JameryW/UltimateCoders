# D6 — Graph cutover ownership

Status: decided
Decided: 2026-11-07

## Question

Should the V8 Agent OS upgrade flip `UC_GRAPH_SHADOW` (or graph-only authority) on by default in this effort?

## Decision

**No code default flip in P0.** Graph-primary cutover remains **ready-for-human**: ops runbook + explicit authorization. Existing durable-runtime maps stay authoritative for that work.

## Why

Lease/fence/commit-once code largely exists; the risk is cutover/ops, not greenfield. Coupling it to Decision Record would block the audit trail on an unrelated ops decision.
