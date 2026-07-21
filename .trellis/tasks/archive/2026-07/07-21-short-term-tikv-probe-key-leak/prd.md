# short-term-tikv-probe-key-leak

## Goal

`ShortTermMemory::new` (`crates/uc-engine/src/memory/short_term.rs:59`)
fire-and-forgets the delete of the `__uc_probe_<uuid>` write-probe key:
```rust
let _ = client_arc.delete(probe_key).await;
```
If the delete fails (TiKV transient error), the probe key leaks into TiKV
permanently. Low volume per startup, but accumulates across restarts.

## What I already know

- Bug site: `short_term.rs:58-60` (probe write → fire-and-forget delete).
- Storage-feature gated; TiKV path only.
- Probe key format: `__uc_probe_<uuid>` (unique per attempt).

## Requirements

- On delete failure: log a warn (key + error) so the leak is visible, not
  silent. No retry — a probe key leak is trivial volume; visibility is the
  fix.

## Acceptance Criteria

- [ ] Delete failure logged (not silent `let _ =`).
- [ ] `cargo check/clippy/fmt -p uc-engine` clean (storage feature).
- [ ] Existing engine tests green.

## Definition of Done

- Fix applied.
- PR opened + CI green + merged.

## Out of Scope

- Delete retry/backoff (probe leak is trivial; visibility suffices).
- Other scan findings.

## Technical Notes

- `crates/uc-engine/src/memory/short_term.rs:55-75` (probe retry loop).
- Feature: `storage`.
