# 08: approx prune-only path and auto-disable

**What to build:** An approx scorer that emits discard/keep only, wired as a pre-filter before full Oracle (workflow / MetaInfer candidate fan-in — not inside Oracle evaluate). Prune runs only when enforce flag and agreement arming (ticket 07) are both on. Public uplift helpers must refuse approx scores. Bench or harness identity change forces re-agreement; failure auto-disarms approx.

**Blocked by:** 07 (rank-agreement gate + arming latch).

**Status:** ready-for-agent

- [ ] Approx scorer returns prune decisions only (never OracleVerdict / public_uplift)
- [ ] Wire prune-only upstream of Oracle; survivors still take full BenchmarkRunner measure
- [ ] Uplift / report helpers reject approx-sourced numbers
- [ ] Retest-on-change path clears arming when agreement fails
- [ ] Tests cover armed vs unarmed prune and uplift refusal
- [ ] Feature flag defaults off; disarm is automatic on retest fail
