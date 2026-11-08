# D16 — Next side-effect receipt surface: MetaInfer kill/control

Status: decided
Decided: 2026-11-08

## Question

V8 P1 landed exactly one non-git EffectLedger kind (`metainfer-create`, D7). The
assessment still records "broad tool receipts still open" / "general tool receipts
still thin". Which operation is the next receipt surface — and should MCP
`write_file` receipts be that surface?

## Decision

**Next kind = MetaInfer kill/control POST (`kind="metainfer-kill"`), behind
`UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off / unset = off).**

1. **Pick it by failure shape, not by "one line item left".** The termination path
   today only reports `RemoteStateUncertainError("termination unconfirmed; stop it
   at the service")` when the control POST times out or the connection drops. That
   is exactly the ledger's `UNKNOWN` semantic — "did the kill happen?" is
   undecidable — and there is no receipt, so `preserve_effects` / the watchdog
   cannot see whether termination was ever confirmed.
2. **Explicitly do NOT pick MCP `write_file` receipts** (a P1 out-of-scope item).
   A local write has no lost-response / double-apply failure shape — the very shape
   the ledger exists for (same reasoning as D7). `uc-fs` also runs as a separate
   stdio subprocess and holds no `RuntimeState`, so a receipt there would need a
   new out-of-band pipeline; that collides with the repository's "no new pipeline"
   discipline. A `write_file` receipt is **evidence**, not exactly-once — it belongs
   to the Decision Record / audit surface and gets its own decision ticket later.
   → Landed as **D17 / P7**: attempt-scoped `terminal.workspace` evidence derived from
   the git diff, not per-tool interception.
3. **Do not pick "replace `remote_jobs` with EffectLedger"**: D7 already put that
   out of scope; `remote_jobs` stays the operational store.

## Why

- The kill POST is the **last** side effect inside the MetaInfer boundary that has
  the ledger's failure shape: an external HTTP mutation whose response may be lost
  and whose repeat has a cost.
- It reuses the create path's already-proven sequence
  (`prepare` → `mark_in_flight` → `confirm` / `fail` / `mark_unknown`); no new state
  machine, no new storage, no new pipeline.
- Flag off ⇒ zero `tool_effects` rows and a byte-identical termination path, matching
  the default-off discipline of D7 / P1.

## Boundary (what this decision does not do)

- No create-path change, no `remote_jobs` semantic change, no digest bump.
- The receipt is produced but **not** wired into `preserve_effects` consumption —
  that consumer is a later ticket.
- A failed quiescence proof after a confirmed kill is not re-classified: the kill
  receipt stays `CONFIRMED`, and the existing `cleanup_pending` /
  `RemoteStateUncertainError` path is unchanged.

## Implementation ticket

T24: wrap the termination path in the ledger — `CONFIRMED` skips a repeat POST,
`UNKNOWN`/`RECONCILING` refuse the retry, `ok != true` becomes `FAILED`, and a
transport failure becomes `UNKNOWN`. Native edge T24 ← D16.
