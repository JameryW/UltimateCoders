# D5 — Decision Record shape

Status: decided
Decided: 2026-11-07

## Question

How should V8 "Decision Record & Audit" land without inventing a second source of truth for routing, context, or governance?

## Decision

One **attempt-scoped Decision Record** (`task:subtask:attempt`) in `RuntimeState` namespace `decision_record`:

- **Start facts**: `worker_id`, optional `route` (omit when identity), `context_digest` + `context_key` (pointer into `context_manifest`, never prompt content), optional governance refs when present on the subtask.
- **Terminal facts**: `success`, compact `error_class` / `summary` (no stdout dumps), optional `oracle` / `review` verdicts when the domain result carries them.
- Start write creates the record; terminal write merges into the same key (CAS mutate). Identical start content is a no-op; terminal always re-stamps `terminal_at`.
- Best-effort: persistence failure never fails the subtask.
- Dashboard read API mirrors context manifests (`task_id` required).

## Why

Route and context already exist as separate trails; auditors need one joinable object per attempt. Pointing at the context manifest avoids duplicating summaries. Attempt in the key matches Context Compiler / checkpoint identity.
