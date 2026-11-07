# 19 — Durable replan accounting wire (T18c)

**What to build:** `WorkCase.replans_used` + `UC_REPLAN_ACCOUNTING_ENFORCE` wiring in stall/`decide` path. Prefer increment on recorded `replan` watchdog verdict. Seams: workcase types, proto, `proactive_service` stall handler, case store CAS.

**Blocked by:** D13

**Status:** implemented (`UC_REPLAN_ACCOUNTING_ENFORCE` default off; `WorkCase.replans_used` + `report_stall` durable path in `proactive_service.rs`)

- [x] Field + proto + persistence
- [x] Flag gate; increment on replan verdict
- [x] Tests for on/off / exhaustion / missing case
