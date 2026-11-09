# 42 — Docs: receipts, reconcile, and scoping in the architecture docs (T42)

**What to build:** The receipts work of P1–P12 (T36 settlement, T37 leases, T38 fencing, D22
merge scope) is described in the maps and agent-os §5, but the two documents a new reader
actually opens — `docs/architecture.md` and `docs/inference-infra.md` — still describe the
old behaviour: no `tool_effects` model at all, and an operator CLI that reconciles
`remote_jobs` without mentioning that it also settles the receipt.

**Blocked by:** none (documentation of decided behaviour)

**Status:** implemented (`docs/architecture.md`, `docs/inference-infra.md`)

- [x] `docs/architecture.md` describes the receipt states, the lease/takeover/fencing
      rules, operator settlement, and what `preserve_effects` may read
- [x] `docs/inference-infra.md` says `attach`/`confirm_stop` settle the receipt and print
      `settled_effects`, and that a `FAILED` receipt is never rewritten
