# 01 — Decision Record outbox + worker write

Status: implemented
Blocked by: none

## What to build

Implement `DecisionRecordOutbox` and wire the Worker to write start + terminal facts per D5 / [spec-p0.md](../spec-p0.md).

## Acceptance criteria

- `decision_key(task_id, subtask_id, attempt) -> "{task}:{subtask}:{attempt}"`
- `record_start(key, *, worker_id, route=None, context_digest=None, context_key=None, governance=None)` creates/replaces start facts; identical start payload is a no-op preserving `recorded_at`
- `record_terminal(key, *, success, summary=None, error_class=None, oracle=None, review=None)` merges terminal facts and sets `terminal_at`
- Worker calls start after route (+ context digest when a manifest was compiled) and before workspace acquire; calls terminal on attempt completion (success, final failure, and early worktree-allocation / quarantine finishes)
- Digest is a stable SHA-256 of the canonical JSON of the context manifest summary (or omit when no manifest)
- Tests in `tests/python/test_decision_record.py` (and worker persistence best-effort coverage)
