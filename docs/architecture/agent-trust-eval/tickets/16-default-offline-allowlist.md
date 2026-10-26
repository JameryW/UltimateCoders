# 16: default-toward-offline and LLM allowlist

**What to build:** With NetworkMode enforcement present (ticket 15), add opt-in default toward offline / Restricted and an explicit LLM/API host allowlist for Restricted mode. NATS/Gateway remain worker-side. Update dsec / sandbox defaults only behind flags — never silently change Full deployments.

**Blocked by:** 15 (real NetworkMode enforcement).

**Status:** implemented (branch `cursor/t16-default-offline-allowlist-7c92`; `UC_NETWORK_DEFAULT_RESTRICTED` + `UC_NETWORK_ALLOWLIST` + optional `UC_NETWORK_DEFAULT_MODE`; Restricted applicator `plan_restricted_allowlist` with `backend=allowlist_preflight` registered at `SandboxManager.__init__`; construction sites use `resolve_network_default()`; OS selective egress deferred/honest; flags default off; tests in `tests/python/test_network_allowlist.py`)

- [x] Opt-in default Restricted/None policy behind a flag (default off)
- [x] Restricted mode consults an explicit host allowlist for LLM/API
- [x] Worker NATS/Gateway path not placed on the child allowlist requirement
- [x] Flag off preserves prior defaults
- [x] Tests cover allowlist allow/deny for Restricted
- [x] Feature flag(s) default off

**Honesty note:** Restricted MVP is policy fail-closed (`allowlist_preflight`, noop argv). Kernel selective egress (nft/proxy) is a follow-up — not claimed as shipped.
