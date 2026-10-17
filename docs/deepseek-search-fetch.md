# DeepSeek Search / Fetch (UC first-party)

UltimateCoders exposes DeepSeek’s official standalone web APIs as tools on
both the **Worker** (sandbox MCP via `engine_mcp`) and the **OMP Extension**
(`uc_deepseek_web_search` / `uc_deepseek_web_fetch`).

This is independent of `deepseek-harness` / `dsh` headless coding. Harness
behavior is unchanged.

## Enablement

Set `DEEPSEEK_API_KEY`. Tools are registered only when the key is present.
Calls fail closed (actionable error, no silent empty success) if the key is
missing at call time or the API returns 401/402/403/404/429/5xx.

Optional: `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`). Trailing
slashes are stripped; UC appends `/v1/tools/search` and `/v1/tools/fetch`.

## Surfaces

| Surface | Tool names | Implementation |
|---------|------------|----------------|
| Worker MCP (`python -m ultimate_coders.agent.engine_mcp`) | `deepseek_web_search`, `deepseek_web_fetch` | Shared Python client |
| OMP Extension | `uc_deepseek_web_search`, `uc_deepseek_web_fetch` | Thin TS bridge → `python -m ultimate_coders.agent.deepseek_web` |

`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` are on the sandbox **shared** env
allowlist so every coding-agent subprocess can reach the uc-engine MCP tools
(same pattern as hy-memory). This does not change `deepseek-harness` itself.

Override the Python executable for OMP with `UC_PYTHON` (default `python3`).

## Fetch URL policy (UC precheck)

Before calling DeepSeek Fetch, UC requires:

- `https:` only (plain `http:` rejected)
- No embedded username/password
- Hostname not localhost / `*.localhost` / `*.local` / common metadata names
- After DNS resolution, only globally routable addresses are allowed
  (blocks loopback, RFC1918, CGNAT 100.64/10, link-local, unique-local,
  reserved/multicast, and common cloud-metadata addresses)
- Unresolved or ambiguous non-global targets fail closed

## Limits and billing

| Item | Value |
|------|-------|
| Search QPS | 5 |
| Fetch QPS | 5 |
| Price | $0.75 / 1 000 successful HTTP 200 requests (each API) |
| Grace | Agent-embedded Search/Fetch free until **2026-11-01** (Beijing) |

Official guide: <https://api-docs.deepseek.com/guides/search>

## Rollback

Unset `DEEPSEEK_API_KEY` (or remove it from worker/OMP env). Tools disappear
from registration; no other UC paths depend on this client.
