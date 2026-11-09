# 54 — A drained client takes its publisher with it (T54)

**The bug.** `_close_owned_nats` cleared `_nats_client` but left `_nats_publisher`
wrapping it. `NatsPublisher` holds the client (`_nc`), so after a close the publisher kept
pointing at a **drained** connection — and `_retry_connect_once` (T48) only builds a
publisher when there is none, so a stale one would never be replaced. The retry would
report success while every publish went into a closed socket.

**Fix.** When the client being dropped is the one the publisher wraps, drop the publisher
too (identity check, so a publisher belonging to an injected client — whose client this app
does not own — is left alone). The next retry then builds a fresh publisher over the new
client.

**Proof.** Three tests in `tests/python/test_dashboard_nats_binding.py`:

- dropping an owned client clears the publisher bound to it;
- a publisher bound to a *different* (injected) client survives the shutdown;
- after a close, `_retry_connect_once()` rebinds a fresh publisher over the **new** client.

The first and third were run against the unfixed file (`git stash` on `app.py`) and fail
there — the tests catch the bug rather than describing the fix.

**Status:** implemented (`python/ultimate_coders/dashboard/app.py`,
`tests/python/test_dashboard_nats_binding.py`)

**Boundary:** shutdown itself is unchanged (drain, then clear); nothing here reconnects on
close — the retry loop remains the only path that establishes a new client.
