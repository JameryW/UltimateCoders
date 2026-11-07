# 19 — Durable replan accounting wire (T18c)

**What to build:** `WorkCase.replans_used` + `UC_REPLAN_ACCOUNTING_ENFORCE` wiring in stall/`decide` path. Prefer increment on recorded `replan` watchdog verdict. Seams: workcase types, proto, `proactive_service` stall handler, case store CAS.

**Blocked by:** D13

**Status:** ready

- [ ] Field + proto + persistence
- [ ] Flag gate; increment on replan verdict
- [ ] Tests for on/off / exhaustion / missing case
