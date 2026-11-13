# 61 — The LLM concurrency cap is claimed, not checked (T61)

**Survey first.** Production `unwrap`/`expect` sites across `crates/**` (tests stripped):
**30** in 11 files — lock poisoning in simple arithmetic sections, invariant assertions
(`expect("fenced matched above")`), constant `NaiveTime`s, and constructor-misuse panics.
No panic reachable from untrusted input, so the panic surface is fine.

**The defect the survey did not look for** — a plain load-then-add in the rate limiter:

```rust
let active = self.active_count.load(Ordering::SeqCst);
if active >= self.max_concurrent { return Err(RateLimited(1)); }
...
self.active_count.fetch_add(1, Ordering::SeqCst);   // ← much later
```

Every thread that observed `active < max` passes the check, so **the cap is advisory**:
under contention `max_concurrent` requests plus however many threads slipped through the
window. `max_concurrent` is the LLM API's concurrency limit, so exceeding it is exactly the
kind of burst a limiter exists to prevent.

Second, smaller half: `release()` used `fetch_sub(1)`. An unmatched release wraps the
counter to `usize::MAX` — after which `active >= max` is always true and the limiter
**refuses every request forever**.

**Fix.** Claim the slot atomically with `AtomicUsize::fetch_update` (`current < max` →
`current + 1`), release the claim on either bucket rejection, and make `release()`
saturating so it cannot wrap. The RPM refund added earlier (T-notes in the code) is kept.

**Tests** (`crates/uc-engine/src/rate_limiter.rs`):

- `concurrency_cap_holds_under_contention` — 32 threads released together against a cap of
  4: exactly 4 acquire, and `active_count() == 4`;
- `a_rejected_acquire_does_not_hold_a_slot` — TPM rejects the request; the claimed slot is
  released and no request is counted;
- `releasing_without_acquiring_does_not_wrap_the_counter` — an unmatched release leaves the
  counter at 0, not `usize::MAX`.

**Status:** implemented (`crates/uc-engine/src/rate_limiter.rs`)

**Boundary:** `can_request()` still reads the counter without claiming it, by design — it is
a hint for callers, and `try_acquire` is what enforces. `cargo test`/`cargo clippy`/
`cargo fmt` on this crate are the verification (Rust CI); the local sandbox cannot reach
crates.io, so no local build was possible.
