# 62 — The token buckets cannot be panicked into (T62)

**Survey result first, because it changes what is worth doing.** Production
`unwrap`/`expect` (tests stripped), per crate:

| crate | production | in tests |
|---|---|---|
| `uc-grpc` | **0** | 480 |
| `uc-grpc-server` | **0** | 63 |
| `uc-engine` | 26 | 1105 |
| `uc-types` | 2 | 115 |

The gateway and its binary have **no** production panic sites at all. What remains is in
`uc-engine`, and most of it is lock poisoning over values that cannot be invalid.

**Except one.** `TokenBucket`'s six `.lock().unwrap()` were assumed safe "because the
critical section is arithmetic" — but `wait_time` did this **while holding the `tokens`
lock**:

```rust
let seconds = (amount - *tokens) / self.refill_rate;
Duration::from_secs_f64(seconds)   // panics on NaN, negative, or overflow
```

A non-finite `estimated_tokens` (a token estimate is arithmetic over model output, and
`f64::NAN` compares false with every bucket) walks straight into that panic **under the
lock**. `std::sync::Mutex` then poisons, and every later `try_consume` / `available` /
`refill` panics too: one bad request turns the rate limiter into a permanent panic source
for the process — the same "permanent damage from one transient failure" shape as T54/T56/
T57, now inside the Rust engine. `Instant::duration_since` is a second, smaller path: it
panics if the monotonic clock ever reads earlier than the stored instant.

**Fix.**

1. `try_acquire` rejects a non-finite or negative estimate with
   `EngineError::InvalidOperation` before any bucket is touched.
2. `wait_time` only calls `Duration::from_secs_f64` on a finite, positive value; anything
   else returns the same 3600s answer an unrefilling bucket already gave.
3. `refill` uses `saturating_duration_since`, so a clock that steps back means "no time
   passed" instead of a panic.
4. All six bucket locks go through `lock_unpoisoned`, so a panic anywhere can no longer
   cascade into every later request.

**Tests** (`crates/uc-engine/src/rate_limiter.rs`):

- `a_non_finite_token_estimate_is_rejected_not_panicked_on` — `NaN`, `INFINITY` and `-1.0`
  are errors, hold no slot, and the limiter still acquires afterwards (which it could not
  if the locks had been poisoned);
- `wait_time_survives_a_non_finite_expectation` — `wait_time(NAN)` and `wait_time(INFINITY)`
  return 3600s instead of panicking in `from_secs_f64`.

**Status:** implemented (`crates/uc-engine/src/rate_limiter.rs`)

**Boundary:** behaviour is unchanged for finite, non-negative estimates. `cargo fmt` is
clean locally; `cargo check`/`clippy`/`test` (the build evidence) run in Rust CI, because
this sandbox cannot reach crates.io.
