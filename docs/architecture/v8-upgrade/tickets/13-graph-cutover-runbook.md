# 13 — Graph cutover ops runbook

**What to build:** Author `docs/architecture/graph-cutover-runbook.md` (or under `v8-upgrade/`) covering prerequisites, enabling `UC_GRAPH_SHADOW=on`, verification, rollback, and explicit “defaults stay off” language. Link from V8 map P4 and the durable-runtime section of `docs/architecture.md`. Cite `durable-runtime-upgrade-notes.md` for lockstep restart.

**Blocked by:** D6, D10

**Status:** ready

- [ ] Runbook exists and is linked from map + architecture.md
- [ ] States exact `on` gate and that Compose defaults remain off
- [ ] Rollback section present
