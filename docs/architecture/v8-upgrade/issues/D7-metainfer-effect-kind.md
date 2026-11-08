# D7 — First non-git EffectLedger kind

Status: decided
Decided: 2026-11-07

## Question

Which mutating operation should be the first non-git EffectLedger kind for V8 P1 tool/side-effect receipts?

## Decision

**MetaInfer create/start POST** (`kind="metainfer-create"`), behind `UC_EFFECT_LEDGER_METAINFER` (default off). Keep `remote_jobs` as the operational store; the ledger is the unified receipt surface for watchdog/`preserve_effects`. Also bind RuntimeState on WorkspaceManager whenever branch push-on-release is enabled so existing git receipts light up on the coding path.

## Why

File writes are local and do not suffer the lost-response / double-apply failure mode. MetaInfer create already classifies ambiguous HTTP as `RemoteStateUncertainError` and is the highest-value non-git external side effect in UC. Full replacement of `remote_jobs` is out of scope.

**Note (2026-11-08, D16 / P6):** the second non-git kind landed — `metainfer-kill` behind `UC_EFFECT_LEDGER_METAINFER_CONTROL` (default off). Local `write_file` receipts stay deliberately out of the ledger: they are evidence, not exactly-once. See [D16](D16-metainfer-control-receipt.md).
