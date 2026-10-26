# 14: same-contract one-shot agent redispatch

**What to build:** When `UC_STALL_REDISPATCH_ENFORCE` is on and a stall signal fires, redispatch the same contract digest once with an alternate registered Agent / binding. Preserve digest identity (must not look like `contract_changed`). After the second failure, escalate to human / non-retryable terminal. Quotas: exactly one swap per stall episode.

**Blocked by:** 13 (stall signal surface).

**Status:** ready-for-agent

- [ ] One alternate-agent redispatch under unchanged contract digest
- [ ] Second failure escalates (no further automatic swaps)
- [ ] Does not bump contract version or rewrite goal text
- [ ] Progress detector still has no replan API
- [ ] Tests cover swap-once + escalate and flag-off no-swap
- [ ] Feature flag defaults off
