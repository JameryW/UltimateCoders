# D1 — V8 scope posture

Status: decided
Decided: 2026-11-07

## Question

Does adopting the V8 diagram mean UltimateCoders becomes a full Agent+Inference+Training product, or a selective hardening of existing domains?

## Decision

**Selective hardening.** V8 is a checklist for the Agent OS, Decision/Context/StateOS, and Research/Evidence planes UC already owns. Inference Runtime and Learning OS stay outside the product shape unless a later decision re-opens them.

## Why

UC's durable runtime, governance, MetaInfer acceptance boundary, and trust/eval gates already encode the V8 correctness constraints that matter for distributed coding. Growing an in-tree GPU/training stack would violate existing MetaInfer isolation and dilute the coding control-plane product.
