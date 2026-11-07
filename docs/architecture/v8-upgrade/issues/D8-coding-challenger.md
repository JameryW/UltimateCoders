# D8 — Coding Champion/Challenger arena

Status: decided
Decided: 2026-11-07

## Question

How should V8 Research/Evidence "Champion/Challenger" land for **coding agents** without inventing a GPU search arena or duplicating stall redispatch?

## Decision

Flag-gated **same-contract one-shot challenger** after a champion **Completed** success:

- Flag: `UC_CODING_CHALLENGER_ENFORCE` (default off)
- Optional challenger name: `UC_CODING_CHALLENGER_AGENT` (else first registry alternate ≠ champion)
- Episode key `_uc_challenger_episode`: ≤1 challenger attempt per `(subtask, contract_digest)`
- Orchestrator-owned (mirror stall redispatch): reopen Pending, stamp `agent_config["agent"]` + `eval_role=report`, bump `dispatch_retry_count`, never rewrite `user_request`
- **Compare / observe only** in MVP — no auto-promote of challenger over champion; a failed report-role challenger must not demote the champion to Failed
- Stall-path agent swap remains `UC_STALL_REDISPATCH_ENFORCE` (T14); challenger does not replace it (and stall must not run on the report-role challenger attempt)

## Why

Trust/eval already has dual-gate / completion / typed review for evidence. Stall redispatch already covers failure/stall swaps. The missing Evidence OS piece is a **report-role** challenger after a successful champion, without Learning OS or MetaInfer Oracle.
