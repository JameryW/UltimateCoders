# 52 — Prove the receipts endpoint's auth over HTTP (T52)

**What to build:** The P13 spec listed "auth" among the tests for
`GET /dashboard/api/effects`, and the acceptance criteria say "unauthenticated call is
refused like the other API routes" — but every test called the handler with a
`127.0.0.1` client, which `_check_auth` **bypasses by design**. So the 401 path had no
test at all, and could not have had one at that level.

New HTTP-level tests (`TestClient`) with `DASHBOARD_PASSWORD` set — `TestClient` reports a
non-localhost host, so the password is genuinely enforced:

- no token → `401 {"error": "Unauthorized"}` and no `effects` key;
- wrong token → `401` (a 401 must not be a formality);
- `Authorization: Bearer` → `200` with the seeded receipt;
- `?token=` → `200`;
- bad `limit` / unknown `status` / `stale` without `RECONCILING` → `400` over HTTP;
- paging round-trip over HTTP: `next_after` feeds the next request.

**Status:** implemented (`tests/python/test_dashboard_effects.py`; spec-p13 item 2 now
records why auth must be proved over HTTP)

- [x] 401 without a token, and with a wrong token
- [x] 200 with a Bearer header and with a query token
- [x] 400s and cursor paging verified through the real routing/serialization path

**Boundary:** the localhost bypass itself is not changed or questioned here — it is the
documented rule for other routes too. This task only makes the remote-caller path provable.
