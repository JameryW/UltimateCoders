# 48 — Dashboard NATS retry can no longer die silently (T48)

**What to build:** `_spawn_nats_connect_retry`'s loop treated "connected" as success and
subscribed afterwards. If `_subscribe_nats_subjects()` raised, the task ended with an
unretrieved exception, `_nats_client` was already set so nothing retried, and the dashboard
silently stopped receiving NATS events — the only trace was a GC warning. The publisher it
had just created also stayed bound to the closed client.

**Status:** implemented (`python/ultimate_coders/dashboard/app.py`; tests in
`tests/python/test_dashboard_nats_binding.py`)

- [x] `_retry_connect_once()` claims success only after subscribe; on failure it drops and
      closes the client, clears a publisher it created, and reports failure
- [x] The loop keeps retrying after a failed subscribe (test drives two attempts)
- [x] A retry task that dies for any other reason logs at warning from its done callback

**Not decided here:** retry cadence, backoff and alerting are unchanged (10s fixed sleep);
this is the "don't lose the failure" half only.
