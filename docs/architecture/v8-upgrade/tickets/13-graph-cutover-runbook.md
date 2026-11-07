# 13 — Graph cutover ops runbook

**What to build:** Author `docs/architecture/graph-cutover-runbook.md` (or under `v8-upgrade/`) covering prerequisites, enabling `UC_GRAPH_SHADOW=on`, verification, rollback, and explicit “defaults stay off” language. Link from V8 map P4 and the durable-runtime section of `docs/architecture.md`. Cite `durable-runtime-upgrade-notes.md` for lockstep restart.

**Blocked by:** D6, D10

**Status:** implemented (branch `cursor/v8-graph-cutover-runbook-e6a9`; runbook at `docs/architecture/graph-cutover-runbook.md`; linked from map P4 + `docs/architecture.md` durable-runtime section; exact `on` gate + Compose defaults stay off; rollback + durable-runtime-upgrade-notes lockstep cited)

- [x] Runbook exists and is linked from map + architecture.md
- [x] States exact `on` gate and that Compose defaults remain off
- [x] Rollback section present
