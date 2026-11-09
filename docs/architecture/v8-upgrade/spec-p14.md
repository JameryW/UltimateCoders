# Spec P14 — Receipt queue view

Blocked by: D24 (decided). Dashboard only: the endpoint already exists (D23).

## What to build

1. **Lib** (`dashboard/src/lib/effects.ts`): `effectsQuery`, `effectAge`,
   `effectOwnerLine`, `effectRequestLine`, `effectHeadline`, `effectsFromPayload`,
   `appendEffectPage`, `effectCountLine`, `EFFECT_STATUSES`.
2. **Panel** (`dashboard/src/components/panels/EffectQueuePanel.tsx`): a pure
   `EffectQueueList` and a fetching `EffectQueue` with a status select, the
   `lease expired` filter for `RECONCILING`, and load-more by `after`.
3. **View registration**: `TerminalView` gains `effects`, `TERMINAL_VIEWS` lists it,
   and the dashboard nav shows it (`收据` / `RECEIPTS`, key `0`).
4. **Tests**: lib helpers and the SSR render of the list (state/age, holder with an
   expired lease, scope line, empty state), plus the existing view-mapping test.

## Acceptance criteria

- [ ] `#/dashboard/effects` resolves to the new view and back.
- [ ] The panel's default query is `status=UNKNOWN&limit=100`.
- [ ] `lease expired` is offered only for `RECONCILING` and adds `stale=1`.
- [ ] Rows show `kind · STATUS · age`, the holder (including "lease expired"), and scope.
- [ ] Load-more appends deduped rows and keeps them when a page fails.
- [ ] `tsc` (app + node) and `vite build` pass; no mutation path exists.

## Out of scope

- Reconcile controls (the CLI owns them)
- Polling/alerting
- Any backend change
