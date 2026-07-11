# ShortTermMemory Lazy Reconnect + TiKV Env Name Fix

## Problem

Dashboard reports `Using in-memory fallback` / `degraded` for short-term memory,
and OMP logs `watchTask stream error: [canceled]`.

Root cause (verified live):

1. **Env name mismatch (primary).** `crates/uc-engine/src/config.rs` reads
   `UC_TIKV_ENDPOINT`, but every script/compose/README/test/Python path uses
   `UC_TIKV_PD_ENDPOINTS`. The gateway container sets
   `UC_TIKV_PD_ENDPOINTS=pd:2379` — unset in the engine → falls back to the
   default `127.0.0.1:2379` → PD unreachable from inside the container →
   `client: None` → permanent in-memory fallback. Python `config.py` had the
   same disease under yet a third name (`UC_TIKV_ENDPOINTS`).

2. **PD advertise-client-urls pointed at 127.0.0.1.** `docker-compose.yml`
   advertised `http://127.0.0.1:2379,http://pd:2379`. Even with the env fix,
   tikv-client asks PD for members and PD returns `127.0.0.1:2379` first →
   client reconnects to the gateway's own loopback → refused.

3. **No runtime reconnect.** `ShortTermMemory` / `LongTermMemory` set
   `client: None` once at startup if the probe fails, and never retry.
   A startup race (storage not yet ready) permanently degrades the engine
   until process restart. (Out of scope for this PR — env fix alone resolves
   the live incident; tracked separately.)

4. **OMP `watchTask [canceled]`.** Not a real bug — OMP's own AbortController
   aborts the stream on exit/restart; a race between abort and the stream's
   error surfaces a `canceled` message that bypasses the
   `ac.signal.aborted` guard at grpc-bridge.ts:419. Noise only.

## Fix (this PR)

- `config.rs`: read `UC_TIKV_PD_ENDPOINTS` (canonical), fall back to
  `UC_TIKV_ENDPOINT` (legacy alias), then default `127.0.0.1:2379`.
- `python/ultimate_coders/config.py`: same canonical name + legacy alias.
- `docker-compose.yml`: PD `--advertise-client-urls=http://pd:2379` only
  (drop `127.0.0.1`).

## Verification

- `cargo check -p uc-engine` passes.
- Rebuild gateway image, recreate container: log shows
  `Connected to TiKV for short-term memory` (no fallback).
- Dashboard health: short_term_memory → `ok`.
