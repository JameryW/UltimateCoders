# 16: default-toward-offline and LLM allowlist

**What to build:** With NetworkMode enforcement present (ticket 15), add opt-in default toward offline / Restricted and an explicit LLM/API host allowlist for Restricted mode. NATS/Gateway remain worker-side. Update dsec / sandbox defaults only behind flags — never silently change Full deployments.

**Blocked by:** 15 (real NetworkMode enforcement).

**Status:** ready-for-agent

- [ ] Opt-in default Restricted/None policy behind a flag (default off)
- [ ] Restricted mode consults an explicit host allowlist for LLM/API
- [ ] Worker NATS/Gateway path not placed on the child allowlist requirement
- [ ] Flag off preserves prior defaults
- [ ] Tests cover allowlist allow/deny for Restricted
- [ ] Feature flag(s) default off
