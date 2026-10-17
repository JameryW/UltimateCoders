# DeepSeek Search/Fetch API integration

## Goal

Add a first-party UltimateCoders Search/Fetch client for DeepSeek’s official standalone APIs (`POST /v1/tools/search` and `POST /v1/tools/fetch`), expose them on **both Worker Python tools and OMP Extension tools** whenever `DEEPSEEK_API_KEY` is set, and keep existing `deepseek-harness` coding unchanged.

## Requirements

* Thin shared Python HTTP client for:
  * `POST {base}/v1/tools/search`
  * `POST {base}/v1/tools/fetch`
* Default base `https://api.deepseek.com`; honor `DEEPSEEK_BASE_URL` when set (strip trailing slash; append `/v1/tools/...`).
* Auth: `Authorization: Bearer $DEEPSEEK_API_KEY`.
* Register tools on:
  * Worker Python tool surface
  * OMP Extension tool surface (bridge into UC Python; do not reimplement HTTP in TypeScript)
* Enablement: register/advertise tools when `DEEPSEEK_API_KEY` is present; fail closed with actionable errors if key missing at call time or API returns 401/402/403/404/429/5xx.
* Fetch URL policy (UC precheck before calling DeepSeek):
  * `https:` only
  * reject embedded userinfo
  * reject localhost, RFC1918, link-local, unique-local, and common cloud metadata addresses after hostname resolution rules agreed in implementation (fail closed on unresolved/ambiguous private targets)
* Preserve `deepseek-harness` headless coding path unchanged.
* Document env, QPS (5/5), and billing notes ($0.75 / 1k successful calls; grace for agent-embedded use ends 2026-11-01).
* Tentative tool names: `deepseek_web_search` / `deepseek_web_fetch` (align with OMP `uc_*` naming if required by existing conventions during implement).

## Acceptance Criteria

* [ ] Client request/response mapping matches official Search/Fetch schemas (search fields, fetch title/content, error envelope).
* [ ] Worker tools and OMP tools both registered when `DEEPSEEK_API_KEY` is configured.
* [ ] Without key: tools absent or calls fail closed (no silent empty success).
* [ ] Fetch rejects non-https, userinfo, and private/metadata targets in unit tests.
* [ ] Unit tests cover request construction, error mapping, and unavailable-key behavior without live paid calls by default.
* [ ] Operator docs / `.env.example` updated.

## Definition of Done

* Tests added/updated
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes
* Rollout/rollback considered if risky

## Technical Approach

1. New Python module (e.g. under `python/ultimate_coders/agent/` or `search/`) implementing Search/Fetch client + URL policy helpers.
2. Worker: register tools alongside existing agent/MCP tool registration when key present.
3. OMP: add a thin bridge (patterned after `memory-bridge` / other `*-bridge.ts`) that invokes the UC-side implementation over the existing RPC/bridge path.
4. Shared tests with httpx mock transport; no live DeepSeek calls in default CI.

## Decision (ADR-lite)

**Context**: DeepSeek now ships dedicated Search/Fetch HTTP APIs; UC needs reusable grounded web access for workers and OMP without SSRF.  
**Decision**: Approach C; Worker + OMP dual surface; auto-enable when `DEEPSEEK_API_KEY` is set; fetch limited to public HTTPS.  
**Consequences**: Billing can start as soon as a key is present and a model calls the tools; operators must treat key presence as enablement. dsh provider switch and Responses `web_search` remain out of MVP.

## Out of Scope

* MetaInfer R8 / GPU acceptance
* Responses API `web_search` (Approach A)
* dsh `DSH_WEB_*` provider switching (Approach B)
* Replacing Exa/other MCP search globally
* Dashboard-first UI
* Plain `http:` fetch
* Explicit `UC_DEEPSEEK_WEB` feature flag (rejected in favor of key-gated enablement)

## Research References

* [`research/deepseek-responses-search-api.md`](research/deepseek-responses-search-api.md)
* [`research/dsh-web-capability-and-uc.md`](research/dsh-web-capability-and-uc.md)

## Technical Notes

* Official guide: https://api-docs.deepseek.com/guides/search
* OMP bridges: `packages/uc-orchestrator/src/orchestrator/*-bridge.ts`
* Worker agent tools: `python/ultimate_coders/agent/`
* DeepSeek chat path: `python/ultimate_coders/agent/llm.py`
* Compose/env: `docker/docker-compose.yml`, `docker/.env.example`

## Implementation Plan (small PRs)

* PR1: Python client + URL policy + unit tests
* PR2: Worker tool registration + docs/env
* PR3: OMP bridge + orchestrator tests/typecheck
