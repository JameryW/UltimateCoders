# 15: NetworkMode real enforcement

**What to build:** Enforce `None` / `Restricted` / `Full` at **agent-subprocess** spawn when `UC_NETWORK_MODE_ENFORCE` is on. Kill fake safety: if enforce is on and the platform cannot apply the mode, refuse start. Do not cut NATS/Gateway on the worker process. Landlock FS is out of scope (ticket 17).

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] Agent child respects None / Restricted / Full when enforce is on
- [ ] Worker control-plane connectivity remains available
- [ ] Unsupported platform + enforce on → fail closed (no silent Full)
- [ ] Flag off leaves today’s spawn behaviour compatible
- [ ] Tests cover at least None vs Full with a child egress probe (or documented CI skip matrix)
- [ ] Feature flag defaults off (`UC_NETWORK_MODE_ENFORCE`)
