# 14: same-contract one-shot agent redispatch

**What to build:** When `UC_STALL_REDISPATCH_ENFORCE` is on and a stall signal fires, redispatch the same contract digest once with an alternate registered Agent / binding. Preserve digest identity (must not look like `contract_changed`). After the second failure, escalate to human / non-retryable terminal. Quotas: exactly one swap per stall episode.

**Blocked by:** 13 (stall signal surface).

**Status:** implemented (branch `cursor/t14-same-contract-agent-redispatch-7c92`; pure helpers in `stall_redispatch.py`; orchestrator Failed→Pending wire via `maybe_apply_stall_redispatch`; flag default off; no replan in `progress.py`)

- [x] One alternate-agent redispatch under unchanged contract digest
- [x] Second failure escalates (no further automatic swaps)
- [x] Does not bump contract version or rewrite goal text
- [x] Progress detector still has no replan API
- [x] Tests cover swap-once + escalate and flag-off no-swap
- [x] Feature flag defaults off
