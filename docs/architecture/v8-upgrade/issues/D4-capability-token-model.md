# D4 — Capability token model

Status: decided
Decided: 2026-11-07

## Question

Should UC introduce portable opaque Capability Tokens as in the V8 diagram, or keep the existing Grant/Mandate model?

## Decision

**Keep Grant/Mandate** (governance Policy Kernel). No portable opaque capability-token protocol in MVP.

## Why

`PrincipalKind` / `Permission` / `Grant` / `Mandate` already express narrowing-only authority with audit. A second token fabric would duplicate the Policy Kernel without closing an observed production gap.
