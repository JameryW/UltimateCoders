# Research: DeepSeek Harness web Search/Fetch vs UltimateCoders wiring

- **Query**: How DeepSeek Harness (`@deepseek-ai/dsh` / deepseek-ai/deepseek-harness) exposes Search/Fetch today, and how UC currently wires dsh; gaps vs Responses API `web_search`
- **Scope**: mixed (internal UC code + upstream dsh docs/API)
- **Date**: 2026-10-17

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/harness_deepseek.py` | In-tree `DeepSeekHarnessAdapter`: `dsh --profile headless`, env forward, `dsh_profile` override |
| `python/ultimate_coders/agent/sandbox.py` | `ADAPTER_ENV_ALLOWLIST["deepseek-harness"]` = `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`; OMP/MiMo tool name maps include `web_search` / `webfetch` (not dsh-specific) |
| `python/ultimate_coders/agent/registry.py` | Loads/registers `harness_deepseek` as builtin plugin |
| `python/ultimate_coders/agent/llm.py` | DeepSeek as LiteLLM chat fallback (`DEEPSEEK_BASE_URL` → `https://api.deepseek.com`); no Responses/`web_search` client |
| `docker/Dockerfile` | Pins `npm install -g @deepseek-ai/dsh@0.2.0-rc.2` |
| `docker/docker-compose.yml` | Worker/orchestrator env: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` only |
| `docker/.env.example` | Documents deepseek-harness agent + those two vars |
| `tests/python/test_agent_plugin_registry.py` | Adapter contract tests (profile, key, base URL) |
| `tests/python/test_sandbox_env_allowlist.py` | Allowlist coverage for `deepseek-harness` / `DEEPSEEK_API_KEY` |
| `.trellis/spec/backend/agent-capability-spec.md` | Capability advertising includes `deepseek-harness` / `deepseek` |
| `.trellis/tasks/10-17-deepseek-search-fetch/prd.md` | Task PRD noting dual surfaces (Responses vs dsh web seam) |

### 1. Upstream web capability seam (`ctx.web`)

Upstream docs: [docs/subsystems/web.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/web.md).

**Architecture (capability seam, not agent-loop spine):**

| Layer | Package(s) | Role |
|---|---|---|
| Service | `@deepseek-ai/dsh-web` | `ctx.web` registries + `search()` / `fetch()` + selection errors |
| Search providers | `dsh-web-search-deepseek`, `dsh-web-search-exa`, `dsh-web-search-perplexity`, `dsh-web-deepseek-api` | Register `WebSearchProvider`s |
| Fetch providers | `dsh-web-fetch-http`, `dsh-web-deepseek-api` | Register `WebFetchProvider`s |
| Model tools | `@deepseek-ai/dsh-tool-web` | Exposes `web_search` and `web_fetch` tool schemas/prompts |

**Request/result shapes (seam):**

- Search: `{ query, maxResults? }` → `{ content?, sources[{url,title?,snippet?,publishedAt?}], truncated }`
- Fetch: `{ url }` → `{ url, statusCode, body: html|text, truncated }` (non-2xx HTTP is a result, not a throw)

**Provider selection (execution time):** configured id wins; else exactly one usable provider; else `WEB_PROVIDER_AMBIGUOUS` / `WEB_PROVIDER_UNAVAILABLE`. Env equivalents: `$DSH_WEB_SEARCH_PROVIDER` / `$DSH_WEB_FETCH_PROVIDER` feed the same `searchProvider` / `fetchProvider` fields (not a separate priority chain).

#### Shipped providers relevant to DeepSeek

**A. `dsh-web-search-deepseek` → provider id `deepseek-official`**

- [README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-search-deepseek/README.md)
- Calls Anthropic-compatible Messages API with native server tool `web_search_20250305`
- Default base: `https://api.deepseek.com/anthropic/v1` (+ `/messages`); override via config `baseURL` or **`$DEEPSEEK_SEARCH_BASE_URL`**
- Does **not** reuse `$DEEPSEEK_BASE_URL` (chat gateway)
- Auth: `DEEPSEEK_API_KEY` (or DeepSeek account token when account route is active)
- Cost model: one full auxiliary model turn per search; defaults `model=deepseek-v4-flash`, `maxUses=5`
- Returns structured `web_search_tool_result` blocks only (no prose scraping)

**B. `dsh-web-fetch-http` → provider id `http`**

- [README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-fetch-http/README.md)
- Anonymous public HTTP(S) fetch: no credentials sent; public-IP resolution + pinning; same-origin redirects only; byte/char/time caps
- Model-facing tool still named `web_fetch` via `dsh-tool-web`

**C. `dsh-web-deepseek-api` → `deepseek-search-api` + `deepseek-fetch-api`**

- [README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/README.md)
- Dedicated Search/Fetch HTTP APIs (no auxiliary model turn)
- Default base: `https://api.deepseek.com/v1/tools` → `/search` and `/fetch`
- Auth: same `DEEPSEEK_API_KEY` / account token
- Search returns titles/URLs/snippets/dates; Fetch returns page Markdown
- Shipped Web UI can switch to these via “Use the new Web Search API”; headless/base defaults do **not** select them unless config/env sets `searchProvider` / `fetchProvider`

#### What `dsh-base` / headless actually mount

From [packages/bundle/base/cordis.patch.yml](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/base/cordis.patch.yml):

```yaml
- id: web
  name: '@deepseek-ai/dsh-web'
  config:
    searchProvider: deepseek-official
    fetchProvider: http
- id: web-search-deepseek
  name: '@deepseek-ai/dsh-web-search-deepseek'
  config:
    apiKeyEnv: DEEPSEEK_API_KEY
- id: web-deepseek-api
  name: '@deepseek-ai/dsh-web-deepseek-api'
  config:
    apiKeyEnv: DEEPSEEK_API_KEY
- id: web-fetch-http
  name: '@deepseek-ai/dsh-web-fetch-http'
- id: tool-web
  name: '@deepseek-ai/dsh-tool-web'
  config:
    fetch: true
    searchTimeoutMs: 60000
```

So the **default product composition already enables model tools `web_search` + `web_fetch`**, with Messages-based DeepSeek search + local HTTP fetch. The dedicated API providers are registered but not selected by default.

**Headless profile implications** ([packages/bundle/headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md), [cordis.patch.yml](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/cordis.patch.yml)):

- Headless is a one-shot patch **over `dsh-base`**; it does **not** strip the web rows.
- Disables managed credential store / authorization / deepseek-account → API-key routes use launch env, then `/.env`, then `$DSH_HOME/.env`.
- Contract: `dsh --profile headless "<task>"` → final assistant text on stdout, exit 0 on success; optional `--json`, `--session-id`.
- No GUI / no listening port; same model-facing tools as base unless further patched.
- Some agent presets (e.g. Cordis) can set `tool-web` `fetch: false`; that is preset-specific, not the headless default above.

**Config/env summary for enabling or switching providers:**

| Knob | Effect |
|---|---|
| `DEEPSEEK_API_KEY` | Required for `deepseek-official` / `deepseek-*-api` availability |
| `DEEPSEEK_SEARCH_BASE_URL` | Messages search endpoint (independent of chat `DEEPSEEK_BASE_URL`) |
| `DSH_WEB_SEARCH_PROVIDER` | Pin search provider id (`deepseek-official`, `deepseek-search-api`, `exa`, …) |
| `DSH_WEB_FETCH_PROVIDER` | Pin fetch provider id (`http`, `deepseek-fetch-api`, …) |
| Profile / `--patch` / `$DSH_HOME/profiles/.../cordis.patch.yml` | Compose or override web package rows |
| Web UI “Use the new Web Search API” | Selects both `deepseek-search-api` + `deepseek-fetch-api` (GUI path) |

### 2. What UltimateCoders already does

#### Adapter (`harness_deepseek.py`)

```44:46:python/ultimate_coders/agent/harness_deepseek.py
AGENT_NAME = "deepseek-harness"
AGENT_ALIASES = ("deepseek",)
DEFAULT_PROFILE = "headless"
```

- Shells out: `dsh --profile <profile> <prompt>` (default profile `headless`).
- Injects `DEEPSEEK_API_KEY` from `SandboxConfig.api_key` when set.
- Forwards host `DEEPSEEK_BASE_URL` only if present (optional chat/gateway override for dsh).
- Optional `subtask_config["dsh_profile"]` override (tested with `"headless-review"`).
- Preflight: `dsh --profile headless --dump-config`; documents pin hint `@deepseek-ai/dsh@0.2.0-rc.2`.
- Honours `DSH_HOME` when reading credentials format for diagnostics, but does **not** inject `DSH_HOME` into the child env allowlist.

No UC code references `web_search`, `web_fetch`, `ctx.web`, `DSH_WEB_*`, `DEEPSEEK_SEARCH_*`, or `dsh-web-*` packages.

#### Docker pin

```149:164:docker/Dockerfile
# DeepSeek Harness (dsh) — official DeepSeek agent harness CLI; the
# `deepseek-harness` coding agent (UC_CODING_AGENT) shells out to it via
# `dsh --profile headless`. ...
# ... Pinned (not latest): ... 0.2.0-rc.2
RUN ... \
    && npm install -g @deepseek-ai/dsh@0.2.0-rc.2 \
    && dsh --version \
```

#### Compose / env

`docker/docker-compose.yml` (worker ~335–337, and a second service ~427–428) and `docker/.env.example`:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL` (optional)

No `DEEPSEEK_SEARCH_BASE_URL`, `DSH_WEB_SEARCH_PROVIDER`, `DSH_WEB_FETCH_PROVIDER`, or Search/Fetch API base.

#### Sandbox allowlists

```106:106:python/ultimate_coders/agent/sandbox.py
    "deepseek-harness": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
```

Shared allowlist also passes `UC_*` and HTTP(S)_PROXY / NO_PROXY. Names such as `DEEPSEEK_SEARCH_BASE_URL`, `DSH_WEB_SEARCH_PROVIDER`, `DSH_WEB_FETCH_PROVIDER`, `DSH_HOME`, `DSH_TOOLS_MODE` are **not** on the deepseek-harness adapter allowlist and would be stripped from the child process unless added via `UC_SANDBOX_ENV_EXTRA`.

#### Sandbox `web_search` / `webfetch` strings

Appear in OMP builtin tool sets and MiMo/OpenCode tool alias maps (`sandbox.py` ~2359, ~2381–2382, ~2735–2736). These translate **CLI-agent** allow/deny flags for those agents. They are not a UC DeepSeek Search client and are not applied by `DeepSeekHarnessAdapter` (which only builds `dsh --profile …`).

#### LLM layer

`llm.py` routes DeepSeek as an OpenAI-compatible chat provider for planning/fallback. There is no `client.responses.create(..., tools=[{"type":"web_search"}])` (or `/v1/tools/search|fetch`) usage in `python/ultimate_coders/`.

### 3. Gaps: does UC enable web search/fetch for dsh?

| Question | Finding |
|---|---|
| Does UC install a dsh build that includes web tools? | Yes — pinned `@deepseek-ai/dsh@0.2.0-rc.2`; upstream `dsh-base` (inherited by headless) mounts `tool-web` + DeepSeek search + HTTP fetch by default. |
| Does UC explicitly configure or document Search/Fetch? | No. Adapter/compose/docs only mention headless coding + `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`. |
| Does UC select dedicated Search/Fetch APIs (`deepseek-search-api` / `deepseek-fetch-api`)? | No. Would need `DSH_WEB_SEARCH_PROVIDER` / `DSH_WEB_FETCH_PROVIDER` (or profile patch) **and** those vars on the sandbox allowlist. |
| Can operators pass search-specific env today? | Host can set them, but sandbox deny-by-default strips them unless `UC_SANDBOX_ENV_EXTRA` (or allowlist change) includes them. |
| Does UC expose Search/Fetch as first-party Python tools / Responses client? | No. |
| Passive enablement path | With `UC_CODING_AGENT=deepseek-harness` + `DEEPSEEK_API_KEY` in the worker, dsh headless may already offer model tools `web_search`/`web_fetch` via upstream defaults; UC neither verifies nor configures that path. |

**Config/profile pieces that would be needed for explicit Search/Fetch control (inventory only):**

1. Keep or forward `DEEPSEEK_API_KEY` (already done).
2. Optionally forward `DEEPSEEK_SEARCH_BASE_URL` when chat uses a custom `DEEPSEEK_BASE_URL` (search does not inherit it).
3. Optionally set `DSH_WEB_SEARCH_PROVIDER=deepseek-search-api` and `DSH_WEB_FETCH_PROVIDER=deepseek-fetch-api` (and allowlist them) to use dedicated `/v1/tools/{search,fetch}` instead of Messages `web_search_20250305` + HTTP fetch.
4. Optionally profile/`--patch` overlays under `$DSH_HOME` if composition must change beyond env.
5. Allowlist expansion for any of the above env names (or `UC_SANDBOX_ENV_EXTRA`).

### 4. Compare: harness web tools vs UC calling Responses API `web_search`

Sources: [Using the Responses API](https://api-docs.deepseek.com/guides/responses_api); dsh web provider READMEs above.

| Dimension | dsh `ctx.web` + `web_search`/`web_fetch` tools | Responses API `web_search` from UC Python |
|---|---|---|
| **Where it runs** | Inside `dsh` agent loop (model calls tools; providers execute) | UC (or LiteLLM/OpenAI SDK) calls `POST https://api.deepseek.com/...` Responses |
| **Search backends available** | Messages native `web_search_20250305` (`deepseek-official`); dedicated `/v1/tools/search` (`deepseek-search-api`); Exa/Perplexity packages | Server tool `web_search` / `web_search_2025_08_26` on Responses; docs currently: supported with **`deepseek-v4-pro`**; **ignored with `deepseek-flash`** on the live guide |
| **Fetch** | Separate `web_fetch` (HTTP provider or `deepseek-fetch-api` Markdown) | Not a separate Responses “fetch” tool in the public tools table; search actions can include `open_page` / `find_in_page` inside `web_search_call` items (API reference) |
| **UC code today** | Passive: shell to headless; no UC-side tool registration | Absent: no Responses client path for web search |
| **Credentials** | `DEEPSEEK_API_KEY` (+ optional account); search base independent | Same API key; Responses `base_url` `https://api.deepseek.com` |
| **Operator surface** | Profile packages + `DSH_WEB_*` / `DEEPSEEK_SEARCH_*` | Explicit Python/tooling in UC dashboard/worker |
| **Coupling** | Tied to `UC_CODING_AGENT=deepseek-harness` / dsh binary | Can serve any UC agent/tool surface without dsh |
| **Cost shape** | Default official search = aux model turn; API providers = one HTTP call | Server-side search inside Responses turn (model + search tooling) |

**Practical distinction for this task’s PRD options:**

- **(A) Responses API** — UC-owned HTTP client/tools; independent of dsh; docs constrain which models honor `web_search`.
- **(B) Enable/configure dsh web** — mostly already composed upstream for headless; UC gap is env allowlist, provider selection (Messages vs `/v1/tools/*`), and documentation/verification.
- **(C) First-party UC Python wrapping Search/Fetch APIs** — closest to `dsh-web-deepseek-api` endpoints (`/v1/tools/search`, `/v1/tools/fetch`) without running dsh.

### Code Patterns

**UC adapter request construction** (`harness_deepseek.py:171–197`): builds argv `["--profile", profile, prompt]` and env with only DeepSeek chat credentials — no web-provider knobs.

**Sandbox deny-by-default** (`sandbox.py:31–113`): adapter allowlist is the gate for any future `DEEPSEEK_SEARCH_*` / `DSH_WEB_*` forwarding.

**Upstream default selection**: base patch pins `searchProvider: deepseek-official` and `fetchProvider: http` while also registering `dsh-web-deepseek-api` for optional switch.

### External References

- [deepseek-harness web subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/web.md) — seam vocabulary and errors
- [dsh-base cordis.patch.yml](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/base/cordis.patch.yml) — shipped web mounts
- [dsh-headless README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md) — headless contract + credentials-from-env
- [dsh-web-search-deepseek README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-search-deepseek/README.md) — Messages `web_search_20250305`, `DEEPSEEK_SEARCH_BASE_URL`
- [dsh-web-deepseek-api README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/README.md) — `/v1/tools/search` + `/fetch`
- [dsh-web-fetch-http README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-fetch-http/README.md) — anonymous HTTP fetch
- [dsh-tool-web README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/tool-web/README.md) — model tools `web_search` / `web_fetch`
- [DeepSeek Responses API guide](https://api-docs.deepseek.com/guides/responses_api) — `web_search` tool support notes (`deepseek-v4-pro` vs flash)
- Discussion [#408](https://github.com/deepseek-ai/deepseek-harness/discussions/408) — search endpoint does not inherit chat `DEEPSEEK_BASE_URL`

### Related Specs

- `.trellis/spec/backend/agent-capability-spec.md` — `deepseek-harness` / `deepseek` in advertised capabilities when plugin/CLI probe allows
- `.trellis/tasks/10-17-deepseek-search-fetch/prd.md` — open choice among Responses / dsh web / first-party wrappers

## Caveats / Not Found

- Exact package contents of npm `@deepseek-ai/dsh@0.2.0-rc.2` were not unpacked in this environment; findings about “base mounts web” are from current upstream `master` docs/patches and may differ if the pinned rc omitted later web-deepseek-api rows. The Messages search + HTTP fetch + tool-web pattern is documented as the long-standing shipped default.
- No live `dsh --dump-config` was run here (no API key / may lack dsh on PATH in this VM).
- Public Responses guide text observed on 2026-10-17 states `web_search` is supported with `deepseek-v4-pro` and ignored with `deepseek-flash`; earlier archive notes in the PRD mentioning flash-class tooling may be stale relative to the live guide.
- No `.trellis/spec` document dedicated to DeepSeek Search/Fetch exists yet.
- UC does not currently call `/v1/tools/search` or `/v1/tools/fetch` directly from Python.
