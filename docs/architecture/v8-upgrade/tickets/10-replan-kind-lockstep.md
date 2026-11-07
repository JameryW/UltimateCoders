# 10 — ProposalKind `replan` lockstep (Rust + Python)

Status: implemented
Blocked by: none (D9 decided)

## What to build

Add `replan` to Python `ProposalKind`, Rust `ProposalKind::Replan`, `as_str`, and `parse_proposal_kind` (+ round-trip test in proactive_service).

## Acceptance criteria

- Token `"replan"` parses on both sides; unknown tokens still fail closed
- Existing kind tokens unchanged
