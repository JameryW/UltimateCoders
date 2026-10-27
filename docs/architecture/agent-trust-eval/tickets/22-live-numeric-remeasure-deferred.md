# 22: live numeric remeasure deferred

**What to build:** Later follow-on (not P3 MVP): run live SSE (or similar) remeasure in CI / scheduled jobs and refresh pins from measured evidence. Distinct from ticket 21’s offline pin checker. Document the deferral so agents do not block pin CI on a live stack.

**Blocked by:** 21 (pin checker should exist first).

**Status:** deferred (not grabable for P3 MVP)

- [ ] Explicitly out of P3 MVP scope
- [ ] Must not be required for ticket 21 green
- [ ] Future ticket may split SSE live vs GPU/BenchmarkRunner recompute
