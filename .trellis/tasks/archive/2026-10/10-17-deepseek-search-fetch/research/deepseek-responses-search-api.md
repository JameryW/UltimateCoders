# Research: DeepSeek Responses + Search/Fetch APIs

- **Query**: Latest official DeepSeek Search/Fetch APIs for UltimateCoders integration (Responses `web_search`, standalone Search/Fetch HTTP APIs, auth/pricing/streaming/tool_choice, breaking changes vs chat-completions-only docs)
- **Scope**: mixed (official api-docs.deepseek.com + in-repo UC DeepSeek surfaces)
- **Date**: 2026-10-17

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/harness_deepseek.py` | UC `dsh` headless adapter; forwards `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` |
| `python/ultimate_coders/agent/llm.py` | Chat-completions DeepSeek client via OpenAI-compatible base `https://api.deepseek.com`; default model env `DEEPSEEK_DEFAULT_MODEL` → `deepseek-flash` |
| `python/ultimate_coders/agent/sandbox.py` | Env allowlist for `deepseek-harness`; tool name aliases include `web_search` / `webfetch` (CLI-agent naming, not a UC DeepSeek Search client) |
| `python/ultimate_coders/config.py` | Provider key map: `"deepseek": "DEEPSEEK_API_KEY"`; fallback defaults `deepseek` / `deepseek-flash` |
| `.trellis/tasks/10-17-deepseek-search-fetch/prd.md` | Brainstorm notes; assumed Responses `web_search` on flash-class models (superseded by current Pro-only Responses guide text) |

### External References (official)

- [Your First API Call](https://api-docs.deepseek.com/) — base URLs, current model names
- [Using the Responses API](https://api-docs.deepseek.com/guides/responses_api/) — compatibility tables, streaming events, `web_search` support matrix
- [Create Response (`POST /responses`)](https://api-docs.deepseek.com/api/create-response/) — request/response schema
- [Search & Fetch guide](https://api-docs.deepseek.com/guides/search) — standalone Search/Fetch HTTP APIs
- [Search API reference (`POST /v1/tools/search`)](https://api-docs.deepseek.com/api/search)
- [Fetch API reference (`POST /v1/tools/fetch`)](https://api-docs.deepseek.com/api/fetch)
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) — token pricing + Search/Fetch per-request pricing
- [Rate Limit & Isolation](https://api-docs.deepseek.com/quick_start/rate_limit/) — model concurrency limits
- [Tool Calls (Chat Completions)](https://api-docs.deepseek.com/guides/tool_calls) — client-executed function tools only (no built-in web search)
- [Claude Code integration / Web Search](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/) — agent-path Search billing note
- Upstream harness web seam (not a public HTTP API): [deepseek-harness `docs/subsystems/web.md`](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/web.md)

### Related Specs

- `.trellis/spec/backend/omp-tools-spec.md` — UC `Search` / `SearchMemory` (repo/memory search), unrelated to DeepSeek web Search/Fetch
- No `.trellis/spec/**` document currently describes DeepSeek Responses `web_search` or `/v1/tools/{search,fetch}`

---

## 1. Responses API (`POST https://api.deepseek.com/responses`)

### Endpoint and auth

| Item | Value |
|---|---|
| Base URL | `https://api.deepseek.com` |
| Endpoint | `POST /responses` (also reachable via OpenAI SDK `client.responses.create`) |
| Auth | `Authorization: Bearer <DEEPSEEK_API_KEY>` |
| Content-Type | `application/json` |

### Models (current official names)

From [create-response](https://api-docs.deepseek.com/api/create-response/) and [pricing](https://api-docs.deepseek.com/quick_start/pricing/):

| Model ID to send | Serves | Notes |
|---|---|---|
| `deepseek-flash` | DeepSeek-V4.1-Flash-1017 | **Preferred** Flash name |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | Pro |
| `deepseek-v4-flash` (legacy) | Redirected to V4.1-Flash-1017 | Accepted; billed at Flash price |
| `deepseek-v4-flash-vision-exp` (legacy) | Redirected to V4.1-Flash-1017 | Accepted; billed at Flash price |

### `web_search` / `web_search_2025_08_26`

**Current live Responses guide (fetched 2026-10-17):**

| Fact | Detail |
|---|---|
| Built-in tool type documented in create-response | `web_search` (enum also lists `function`, `custom`) |
| Execution | Server-side: API runs search, injects results into context, continues the response |
| Model gate | **Supported with `deepseek-v4-pro`**. **Ignored with `deepseek-flash`** |
| Streaming events for search | `response.web_search_call.in_progress` / `.searching` / `.completed` — documented as **`deepseek-v4-pro` only** |
| Replay | Prior `web_search_call` output items may be passed back in `input`; server restores search results into context |
| Other built-ins | `file_search` / `code_interpreter` / `computer_use` / `mcp` → ignored |
| Custom tool | Only `{"type":"custom","name":"apply_patch"}` (Codex); other custom names → `400` |

**About `web_search_2025_08_26`:** some search-index / cached snippets of the Responses guide still list both `web_search` and `web_search_2025_08_26` (with `search_context_size` / `user_location` ignored, auto-continuation capped at 10 rounds) and `tool_choice` of `{"type":"web_search"}` / `{"type":"web_search_2025_08_26"}`. The **current full create-response reference page** only enumerates tool types `function` / `custom` / `web_search`, and its `tool_choice` named form only documents `{"type":"function","name":...}`. Treat versioned `web_search_2025_08_26` as **historically/compatibility-mentioned**, not as the primary documented tool id on the live OpenAPI page.

**There is no separate public “fetch” tool type** on the Responses API. Page open / in-page find behaviors (when present in older OpenAI-shaped `web_search_call.action` descriptions: `search` / `open_page` / `find_in_page`) are part of the **server-side web_search loop**, not a client-callable `/fetch` Responses tool.

### Request shape (essentials)

```json
{
  "model": "deepseek-v4-pro",
  "instructions": "optional system-level instruction",
  "input": "string OR list of input items",
  "tools": [{ "type": "web_search" }],
  "tool_choice": "auto",
  "stream": false,
  "reasoning": { "effort": "high" },
  "max_output_tokens": 4096,
  "temperature": 1.0,
  "user": "optional end-user id (Responses field name; Chat Completions uses user_id)"
}
```

Rules from docs:

- At least one of `input` / `instructions` required.
- Stateless: no `previous_response_id` / `conversation` / `store` (always `store: false`). Client must resend history.
- `tool_choice`: `none` / `auto` / `required` / specific function `{ "type": "function", "name": "..." }`.
- Unsupported top-level params are **silently ignored** (not errors).

### Response shape (essentials)

- Object type `response` with `status`: `in_progress` | `completed` | `incomplete` | `failed`.
- `output[]` items: `reasoning`, `message`, `function_call`, `custom_tool_call`, `web_search_call`, `tool_search_call`.
- Thinking mode: `reasoning` item precedes `message`.
- Server-side search actions appear as `web_search_call` items.
- `usage`: `input_tokens` (+ `cached_tokens`), `output_tokens` (+ `reasoning_tokens`), `total_tokens`.

### Streaming

- `stream: true` → semantic SSE events (not chat-completions `data: [DONE]`).
- Final event: `response.completed` | `response.incomplete` | `response.failed`.
- Text deltas: `response.output_text.delta` / `.done`.
- Web search lifecycle events exist for Pro (see above).

### Example (SDK)

```python
from openai import OpenAI
client = OpenAI(api_key="<key>", base_url="https://api.deepseek.com")
response = client.responses.create(
    model="deepseek-v4-pro",
    input="Latest Python release notes",
    tools=[{"type": "web_search"}],
)
print(response.output_text)
```

---

## 2. Official non-harness Search/Fetch HTTP APIs (beyond Responses tools)

**Yes — first-party standalone APIs exist** (documented under [Search & Fetch](https://api-docs.deepseek.com/guides/search)):

| API | Endpoint | Purpose |
|---|---|---|
| Search | `POST https://api.deepseek.com/v1/tools/search` | Query DeepSeek’s in-house search engine; structured ranked results |
| Fetch | `POST https://api.deepseek.com/v1/tools/fetch` | Retrieve one URL; return title + Markdown content |

These are **raw JSON APIs**, independent of chat/completions and Responses. Same bearer auth as the rest of the DeepSeek API.

### Auth headers

| Header | Required | Value |
|---|---|---|
| `Authorization` | Yes | `Bearer <DeepSeek API Key>` |
| `Content-Type` | Yes | `application/json` |

### Search request

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | Yes | — | 1–128 Unicode chars; not all whitespace; operators supported |
| `count` | integer | No | `10` | 1–50 |
| `ori_question` | string | No | — | original user question, 1–128 chars |
| `localization` | string | No | `""` | optional IP for localized results |
| `need_content` | boolean | No | `false` | when false, `content` is always `""` |

Query operators: `site:`, `-site:`, `-term`, `-"phrase"`, `"exact phrase"`, `lang:zh,en`, `after:YYYY[-MM-DD]`, `before:YYYY[-MM-DD]`.

### Search response (`HTTP 200`)

```json
{
  "results": [
    {
      "rerank_score": 2.4,
      "title": "...",
      "url": "https://...",
      "snippet": "...",
      "summary": "...",
      "content": "...",
      "site_name": "...",
      "site_icon": "https://...",
      "date": "2026-06-01T00:00:00+00:00",
      "authority": 3
    }
  ]
}
```

- Empty `results` = no matches (still 200).
- `rerank_score` 0–3 (≥1.5 = high quality); `authority` -1..4 (≥3 = highly authoritative).

### Fetch request / response

Request: `{ "url": "https://example.com/article" }`  
Constraints: ≤8192 chars; http(s); host required; **no username/password** in URL.

Response:

```json
{ "title": "Example article", "content": "# Example article\n\n..." }
```

### Errors (Search/Fetch)

OpenAI-compatible error envelope (`param` always `null`). Notable statuses:

| Status | Meaning |
|---|---|
| 400 | Invalid request |
| 401 | Missing/invalid key |
| 402 | Insufficient balance |
| 403 | Fetch rejected by security policy |
| 404 | Fetch could not get usable content |
| 408 | Request body read timeout |
| 413 | Body too large |
| 429 | Search/Fetch QPS exceeded |
| 500 / 504 | Server / downstream timeout |

Retry guidance in docs: retry 408/429/5xx; top up on 402; do not retry other 4xx without changing the request.

### Rate limits (Search/Fetch — separate from model concurrency)

| API | Account QPS |
|---|---|
| Search | 5 |
| Fetch | 5 |

Independent quotas; Fetch also has a **per-domain** QPS limit. Exceeding → HTTP 429.

### Unofficial / third-party paths (not official DeepSeek)

- Blog examples using `https://api.deepseek.com/search` or `GET /v1/search` are **not** the documented endpoints.
- Alibaba Cloud Model Studio’s DeepSeek hosting documents different Responses tools (`web_search`, `web_extractor`, `code_interpreter`) and region-specific model IDs — that is **Alibaba’s product surface**, not `api.deepseek.com`.

---

## 3. Auth, pricing/limits, streaming, tool_choice (summary)

### Auth

- Universal: `Authorization: Bearer <key>` for Chat Completions, Responses, Search, Fetch.
- Anthropic compatibility base: `https://api.deepseek.com/anthropic` (Claude Code path uses `ANTHROPIC_AUTH_TOKEN`).
- UC already wires `DEEPSEEK_API_KEY` / optional `DEEPSEEK_BASE_URL` for harness + LLM client.

### Model token pricing (USD / 1M tokens; peak shown — off-peak is half)

| | Flash | Pro |
|---|---|---|
| Input cache hit | $0.006 peak | $0.044 peak |
| Input cache miss | $0.30 peak | $1.32 peak |
| Output | $1.20 peak | $3.96 peak |
| Concurrency | 2500 | 500 |

Peak UTC windows (Mon–Fri excl. Chinese holidays): 01:00–04:00 and 06:00–10:00.

CNY pricing also published on the zh-CN pricing page (Flash 2/8 元高峰 百万 tokens 输入未命中/输出; Search/Fetch 5 元 / 千次成功请求).

### Search & Fetch pricing

| API | Unit | Price |
|---|---|---|
| Search | Per 1,000 **successful** requests | **$0.75** |
| Fetch | Per 1,000 **successful** requests | **$0.75** |

Billable only on HTTP 200 with a result (Search: non-empty `results`; Fetch: page content). Empty search / failures / timeouts not billed. `count` does not change price.

**Grace period:** server-side Search/Fetch used via DeepSeek Harness / Claude Code / Codex is **currently free**; **per-request billing starts 2026-11-01 00:00 Beijing time**.

Claude Code note: Web Search also incurs **extra model token** cost for summarization rounds, plus Search API billing when billing is on.

### Streaming

| Surface | Behavior |
|---|---|
| Chat Completions | `stream: true`; classic OpenAI chunk stream |
| Responses | Semantic SSE; no `data: [DONE]`; ends with `response.completed|incomplete|failed` |
| Search / Fetch | Non-streaming JSON request/response |

Keep-alive (model APIs): empty lines (non-stream) or `: keep-alive` SSE comments; connection may close if inference has not started after 10 minutes.

### tool_choice

- **Responses:** `none` / `auto` / `required` / specific function. Live create-response docs emphasize function named choice; older snippets also document forcing web search via `{ "type": "web_search" }` (and historically `web_search_2025_08_26`) requiring the tool to be present in `tools` else `400`.
- **Chat Completions Tool Calls:** client-provided `function` tools only; model does **not** execute server-side web search via chat tools.
- **Search/Fetch standalone:** no `tool_choice` — direct HTTP calls.

---

## 4. Breaking / material changes vs older chat-completions-only docs

| Topic | Older / chat-only world | Current official surface |
|---|---|---|
| Primary chat model names | Often `deepseek-chat` / `deepseek-reasoner` / `deepseek-v4-flash` in older materials | Prefer **`deepseek-flash`** and **`deepseek-v4-pro`**; legacy flash names redirected |
| Web grounding | Not a first-class Chat Completions built-in | Two paths: (1) Responses `web_search` on **Pro**; (2) standalone `/v1/tools/search` + `/v1/tools/fetch` |
| Agent integrations | Chat Completions / Anthropic messages | Responses API added for Codex; Claude Code Web Search maps onto DeepSeek Search |
| Stateless multi-turn (Responses) | N/A | Client must resend full `input` history; no server-side conversation store |
| Pricing dimensions | Tokens only | Tokens **plus** Search/Fetch per successful request (billing starts 2026-11-01 for agent-embedded calls) |
| Rate limiting | Model concurrency | Model concurrency **plus** Search/Fetch QPS 5 each |
| UC in-tree client | `llm.py` → chat completions; `harness_deepseek.py` → `dsh` CLI | No UC client yet for `/responses` web_search or `/v1/tools/{search,fetch}` |

### Harness-adjacent (upstream, not UC)

`deepseek-harness` exposes `ctx.web` with `search` + `fetch`, providers including `dsh-web-search-deepseek` / `dsh-web-deepseek-api` / `dsh-web-fetch-http`, model tools `web_search` / `web_fetch`. Official pricing notes that Harness server-side tool calls already hit Search/Fetch. That is a **CLI/harness provider seam**, distinct from calling `api.deepseek.com` HTTP Search/Fetch directly from UC Python.

---

## Code Patterns (UC today)

```20:21:python/ultimate_coders/agent/harness_deepseek.py
    DEEPSEEK_API_KEY   — required for real runs
    DEEPSEEK_BASE_URL  — optional; defaults to the public DeepSeek API
```

```824:833:python/ultimate_coders/agent/llm.py
        elif self.provider == "deepseek":
            if model.startswith("openai/"):
                model = model.removeprefix("openai/")
            elif model.startswith("deepseek/"):
                model = model.removeprefix("deepseek/")
            prepared["model"] = f"openai/{model}"
            prepared.setdefault(
                "api_base",
                os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            )
```

No in-repo references to `/v1/tools/search`, `/v1/tools/fetch`, or `client.responses.create` / Responses `web_search` were found.

## Caveats / Not Found

- Sitemap at api-docs.deepseek.com lists `/api/fetch` but **omits** `/guides/search` and `/api/search` even though both pages return HTTP 200 — discoverability via sitemap alone is incomplete.
- Live Responses guide and create-response OpenAPI disagree slightly with older indexed snippets on `web_search_2025_08_26` and whether Flash can use Responses `web_search` (live text: **Pro only; ignored on Flash**). Prefer the fetched live pages dated this research.
- Exact `web_search_call.action` schema details (`search` / `open_page` / `find_in_page`) appear in some index snippets / create-response highlights but are thin on the full guide page; verify against live responses when implementing.
- No official public **Fetch-as-Responses-tool** separate from Search; standalone Fetch is `/v1/tools/fetch` only.
- UC has **no** existing first-party DeepSeek Search/Fetch HTTP client; only harness env forwarding and chat-completions LLM routing.
