# D3 — Learning OS deferral

Status: decided
Decided: 2026-11-07

## Question

Is Learning OS (trajectory → SFT/RL → 5D+FSDP) in MVP scope for the V8 upgrade?

## Decision

**Out of MVP.** No training loop lands in this effort. A future decision may add a read-only trajectory export handoff; that is not authorized by this map.

## Why

No training OS exists in-repo today. Building one is a product expansion, not an architecture alignment. Prod⊥RL isolation is already satisfied by non-participation.
