# Research: Multi-LLM-Provider Support in AI Coding Agent Systems

- **Query**: How do AI coding agent systems implement multi-LLM-provider support? What abstraction patterns, tool calling normalization, and Python ecosystem libraries exist?
- **Scope**: Mixed (internal codebase analysis + external project research)
- **Date**: 2026-06-18

## Findings

### Current Codebase State (UltimateCoders)

| File Path | Description |
|---|---|
| `python/ultimate_coders/agent/llm.py` | LLMClient class — Anthropic-only, hard-coded |
| `python/ultimate_coders/agent/worker.py` | Worker — uses LLMClient for tool-calling loop |
| `python/ultimate_coders/agent/orchestrator.py` | Orchestrator — uses LLMClient for decomposition |
| `python/ultimate_coders/config.py` | LlmConfig dataclass — provider field exists but only "anthropic" supported |
| `python/ultimate_coders/agent/sandbox.py` | SandboxConfig — agent field ("claude-code"/"codex") maps API keys per agent |

**Key coupling points in current code:**

1. `LLMClient._get_client()` (llm.py:80-103) — hard-coded `anthropic.AsyncAnthropic`, raises ValueError for any other provider
2. `LLMClient._parse_response()` (llm.py:292-322) — parses Anthropic-specific response shape (`block.type == "text"`, `block.type == "tool_use"`, `response.usage.input_tokens`)
3. `LLMClient._format_tool()` (llm.py:324-335) — outputs Anthropic tool format (`name`, `description`, `input_schema`)
4. `LLMClient.complete_with_tools()` (llm.py:140-254) — tool result message format is Anthropic-specific (`tool_use`/`tool_result` content blocks)
5. `LLMClient._call_with_retry()` (llm.py:256-290) — calls `client.messages.create(**params)` (Anthropic SDK method)
6. `config.py:64` — `load_config()` only reads `ANTHROPIC_API_KEY` env var
7. `LlmConfig` (config.py:36-44) — has `provider` and `fallback_model` fields but they are unused

---

### Pattern 1: Aider — Delegation to litellm (Python)

**Architecture**: Aider does NOT implement its own provider abstraction. It delegates entirely to the `litellm` library via a lazy-loading wrapper.

**Key files examined:**
- `aider/llm.py` — LazyLiteLLM class (lazy-loads litellm module)
- `aider/models.py` — Model class (dataclass with per-model settings)
- `aider/sendchat.py` — Message formatting utilities

**How it works:**

1. **LazyLiteLLM** (`aider/llm.py`): A proxy object that defers `import litellm` until first attribute access. This avoids the 1.5s import cost of litellm at startup. All calls like `litellm.completion(...)` go through this proxy.

2. **Model class** (`aider/models.py`): A `@dataclass` extending `ModelSettings` that stores per-model metadata:
   - `name` — canonical model name (e.g., "claude-sonnet-4-6")
   - `edit_format` — which editing strategy to use ("whole", "diff", "udiff", "editor-diff")
   - `weak_model_name` — fallback model for cost savings
   - `use_repo_map`, `cache_control`, `streaming` — capability flags
   - `reasoning_tag` — tag name for extracting reasoning from extended-thinking models
   - `extra_params` — provider-specific kwargs
   - Model settings loaded from `model-settings.yml` YAML file

3. **Provider routing**: Entirely handled by litellm's `model="provider/model-name"` convention. Aider just passes the model name string to `litellm.completion()`.

4. **Tool calling**: Aider uses a single-function tool calling pattern:
   ```python
   kwargs["tools"] = [dict(type="function", function=function)]
   kwargs["tool_choice"] = {"type": "function", "function": {"name": function["name"]}}
   ```
   This is OpenAI-format tool calling, which litellm translates to each provider's native format.

5. **Message normalization**: `sendchat.py` has `ensure_alternating_roles()` to fix message sequences that don't alternate user/assistant (required by some providers, especially Anthropic).

6. **Model aliases**: A `MODEL_ALIASES` dict maps short names to canonical names (e.g., "sonnet" -> "claude-sonnet-4-6", "deepseek" -> "deepseek/deepseek-chat").

**Key insight**: Aider's approach is "don't abstract, delegate." The entire multi-provider complexity is pushed to litellm. Aider only handles model-specific behavioral differences (edit format, caching, reasoning tags) via its ModelSettings dataclass.

---

### Pattern 2: Continue.dev — Abstract Base Class + Per-Provider Subclasses (TypeScript)

**Architecture**: Continue.dev uses an abstract `BaseLLM` class that implements an `ILLM` interface. Each provider (Anthropic, OpenAI, Gemini, etc.) is a concrete subclass.

**Key files examined:**
- `core/llm/index.ts` — `BaseLLM` abstract class implementing `ILLM`
- `core/llm/llms/Anthropic.ts` — Anthropic provider subclass
- `core/llm/llms/OpenAI.ts` — OpenAI provider subclass
- `core/llm/llms/Gemini.ts` — Gemini provider subclass
- `core/llm/toolSupport.ts` — Per-provider tool support detection
- `core/llm/openaiTypeConverters.ts` — OpenAI-format normalization layer

**How it works:**

1. **ILLM interface** defines the contract:
   - `chat(messages, options)` — synchronous chat
   - `streamChat(messages, signal, options)` — streaming chat
   - `complete(prompt, signal, options)` — text completion
   - `supportsFim()`, `supportsImages()`, `supportsCompletions()`, `supportsPrefill()` — capability queries
   - `countTokens(content)` — token counting

2. **BaseLLM abstract class** provides:
   - Common fields: `model`, `apiKey`, `apiBase`, `completionOptions`, `template`, `capabilities`
   - Default implementations for capability queries
   - `providerName` static property for provider identification
   - `underlyingProviderName` for proxy/router scenarios

3. **Per-provider subclasses** override:
   - `convertArgs(options)` — translate CompletionOptions to provider-specific API params
   - `convertMessage()` — translate ChatMessage to provider-specific message format
   - `convertTool()` — translate Tool definitions to provider-specific tool format
   - `_streamChat()` / `_chat()` — actual API call implementation

4. **Anthropic.ts** specifics:
   - `convertToolToAnthropicTool()` — converts generic Tool to Anthropic's `{name, description, input_schema}` format
   - `convertMessageContentToBlocks()` — converts MessageContent to Anthropic's content block format (text/image blocks)
   - Handles Anthropic-specific: system prompt as top-level param, cache control, thinking/reasoning budget
   - Streaming: SSE with Anthropic-specific event types (`RawMessageStartEvent`, `RawContentBlockDeltaEvent`)

5. **OpenAI.ts** specifics:
   - Uses OpenAI SDK's `ChatCompletionCreateParams` format
   - Handles o1/GPT-5 role mapping: `system` -> `developer`
   - Supports both `/chat/completions` and `/responses` API endpoints
   - Tool calling in OpenAI format: `{type: "function", function: {name, parameters}}`

6. **Gemini.ts** specifics:
   - `convertContinueToolToGeminiFunction()` — converts to Google's FunctionDeclaration format
   - `mergeConsecutiveGeminiMessages()` — Gemini requires alternating roles
   - System message handling: removes system message, merges into first user message
   - `useOpenAIAdapterFor` — can delegate to OpenAI-compatible adapter for some operations

7. **Tool support detection** (`toolSupport.ts`):
   - `PROVIDER_TOOL_SUPPORT` — a `Record<string, (model: string) => boolean>` mapping
   - Each provider has a function that checks if a specific model supports tool calling
   - Example: Anthropic supports tools for all models except `claude-2` and `claude-instant`
   - `modelSupportsNativeTools()` — checks capabilities first, then falls back to provider lookup

8. **OpenAI-compatible adapter** (`@continuedev/openai-adapters`):
   - A separate package that provides OpenAI-format normalization
   - `constructLlmApi()` — factory that creates the right API adapter
   - Many providers (Groq, Together, DeepInfra, etc.) use the OpenAI-compatible path

**Key insight**: Continue.dev's approach is "abstract base + per-provider specialization." The `BaseLLM` class defines the contract and provides defaults. Each provider subclass handles format conversion (messages, tools, streaming). A separate `openaiTypeConverters.ts` module normalizes responses back to a common shape. The `toolSupport.ts` module is a practical pattern for capability detection without a full capability registry.

---

### Pattern 3: OpenHands — litellm + Custom Action Execution (Python)

**Architecture**: OpenHands uses litellm for LLM calls and implements its own action/observation system for tool calling.

**Key files identified:**
- `openhands/app_server/utils/llm.py` — LLM utility functions
- `openhands/app_server/utils/llm_metadata.py` — LLM metadata management
- `openhands/app_server/config_api/llm_model_service.py` — Model configuration service
- `enterprise/storage/lite_llm_manager.py` — Enterprise litellm proxy management

**How it works (from codebase structure and litellm integration):**

1. OpenHands uses litellm as its LLM abstraction layer (confirmed by litellm README listing OpenHands as an adopter)
2. The `llm_model_service.py` manages model configurations and profiles
3. Tool calling is handled through OpenHands' own Action/Observation system, not through litellm's tool calling
4. The agent loop: LLM generates actions -> OpenHands executes them -> observations fed back as messages
5. Enterprise tier uses litellm proxy server for centralized key management and routing

**Key insight**: OpenHands separates the LLM call (delegated to litellm) from the tool execution (its own Action/Observation framework). This is a "thin LLM wrapper + thick agent framework" pattern.

---

### Pattern 4: litellm — Universal Translation Layer (Python)

**Architecture**: litellm provides a single `completion()` function that translates between OpenAI-format input and 100+ provider-specific APIs.

**Key files examined:**
- `litellm/main.py` — Public entry point (`completion()`, `acompletion()`)
- `litellm/llms/base.py` — `BaseLLM` class for custom providers
- `litellm/llms/custom_llm.py` — `CustomLLM` class for user-defined providers
- `litellm/litellm_core_utils/get_llm_provider_logic.py` — Provider routing (`get_llm_provider()`)
- `litellm/types/utils.py` — `ModelResponse`, `ChatCompletionMessageToolCall` types

**How it works:**

1. **Provider routing** (`get_llm_provider()`):
   - Model name convention: `"provider/model-name"` (e.g., `"anthropic/claude-sonnet-4-6"`, `"gemini/gemini-2.5-pro"`)
   - Parses the prefix to determine provider
   - Returns `(model, custom_llm_provider, dynamic_api_key, api_base)` tuple
   - Handles special cases: Azure, OpenRouter, Bedrock, etc.

2. **Input format**: OpenAI-compatible
   ```python
   response = litellm.completion(
       model="anthropic/claude-sonnet-4-6",
       messages=[{"role": "user", "content": "Hello!"}],
       tools=[{"type": "function", "function": {"name": "...", "parameters": {...}}}],
   )
   ```

3. **Output format**: Always returns `ModelResponse` (OpenAI-compatible shape)
   - `response.choices[0].message.content` — text content
   - `response.choices[0].message.tool_calls` — tool calls in OpenAI format
   - `response.usage` — token usage

4. **Tool calling translation**:
   - Input: OpenAI-format tools (`{type: "function", function: {name, description, parameters}}`)
   - Output: OpenAI-format tool calls (`{id, type: "function", function: {name, arguments}}`)
   - Internally translates to/from each provider's native format

5. **CustomLLM** for extending:
   ```python
   class CustomLLM(BaseLLM):
       def completion(self, model, messages, api_base, ...) -> ModelResponse:
           # Custom implementation
       def streaming(self, model, messages, ...) -> Iterator[GenericStreamingChunk]:
           # Custom streaming
       async def acompletion(self, ...) -> ModelResponse:
           # Async version
   ```

6. **Provider-specific handling** (from `get_llm_provider_logic.py`):
   - Anthropic: detected by `claude-` prefix pattern matching
   - Azure: `azure/` prefix with special Cohere/Mistral routing
   - OpenRouter: `openrouter/` prefix with nested provider/model parsing
   - Bedrock: `bedrock/` prefix
   - 100+ providers in `litellm/llms/` directory

7. **Streaming**: `GenericStreamingChunk` type for normalized streaming across providers

8. **Key features for production use**:
   - Proxy server (AI Gateway) with virtual keys, spend tracking, load balancing
   - Fallbacks and retries across providers
   - Rate limiting and timeout management
   - `drop_params = True` — automatically drops unsupported params for each provider

---

### Pattern 5: instructor — Structured Output Layer (Python)

**Architecture**: instructor wraps any LLM provider to add Pydantic-based structured output with automatic retries.

**Key features:**
- `instructor.from_provider("openai/gpt-4o")` — provider-agnostic client creation
- `response_model=User` — Pydantic model as response schema
- Automatic retry on validation failures
- Works with OpenAI, Anthropic, Google, Ollama, Groq, etc.
- Uses litellm under the hood for multi-provider support

**Relevance to UltimateCoders**: instructor is primarily for structured extraction, not for agent tool-calling loops. It could be useful for the Orchestrator's decomposition step (parsing JSON subtask arrays) but not for the Worker's tool-calling loop.

---

### Tool Calling Format Differences Across Providers

This is the core challenge for multi-provider support. The three major formats:

#### 1. OpenAI Format
```json
// Tool definition
{"type": "function", "function": {"name": "search", "description": "...", "parameters": {"type": "object", "properties": {...}}}}

// Tool call in response
{"tool_calls": [{"id": "call_abc", "type": "function", "function": {"name": "search", "arguments": "{\"query\": \"...\"}"}}]}

// Tool result message
{"role": "tool", "tool_call_id": "call_abc", "content": "result text"}
```

#### 2. Anthropic Format
```json
// Tool definition
{"name": "search", "description": "...", "input_schema": {"type": "object", "properties": {...}}}

// Tool call in response (content block)
{"type": "tool_use", "id": "toolu_abc", "name": "search", "input": {"query": "..."}}

// Tool result message (content block in user message)
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_abc", "content": "result text"}]}
```

#### 3. Gemini Format
```json
// Tool definition (FunctionDeclaration)
{"name": "search", "description": "...", "parameters": {"type": "object", "properties": {...}}}

// Tool call in response (functionCall part)
{"functionCall": {"name": "search", "args": {"query": "..."}}}

// Tool result (functionResponse part)
{"functionResponse": {"name": "search", "response": {"result": "result text"}}}
```

**Key differences:**
- Tool definitions: OpenAI wraps in `function` key; Anthropic uses `input_schema`; Gemini uses `parameters`
- Tool call arguments: OpenAI serializes as JSON string; Anthropic passes as parsed dict; Gemini passes as `args`
- Tool result routing: OpenAI uses `role: "tool"` messages; Anthropic uses `tool_result` content blocks in `user` messages; Gemini uses `functionResponse` parts
- System prompt: OpenAI uses `system` role messages; Anthropic uses top-level `system` param; Gemini merges into first user message
- Streaming: Each provider has different SSE event types and chunk structures

---

### Common Abstraction Patterns Summary

| Pattern | Used By | Pros | Cons |
|---|---|---|---|
| **Delegate to litellm** | Aider, OpenHands | Zero provider code to maintain; automatic new provider support; battle-tested | Dependency on litellm; less control over provider-specific features; litellm import overhead (~1.5s) |
| **Abstract base class + per-provider subclass** | Continue.dev | Full control; can optimize per-provider; clear separation | More code to maintain; each new provider = new subclass; format conversion code duplicated |
| **OpenAI-compatible adapter** | Continue.dev (secondary), many providers | Many providers already speak OpenAI format; single conversion target | Loses provider-specific features; some providers have incomplete OpenAI compatibility |
| **Custom LLM wrapper** | UltimateCoders (current) | Simple; direct control | Hard-coded to one provider; not extensible |

---

### Python Ecosystem Libraries Comparison

| Library | Purpose | Multi-Provider | Tool Calling | Streaming | Structured Output | Notes |
|---|---|---|---|---|---|---|
| **litellm** | Universal LLM gateway | 100+ providers | Yes (translates to/from OpenAI format) | Yes (sync + async) | No (returns raw) | Python SDK + proxy server; 8ms P95 latency; YC W23; used by Netflix, Stripe, OpenHands |
| **instructor** | Structured output | Yes (via litellm) | Partial (tool-calling for extraction) | Yes | Yes (Pydantic models) | Best for extraction/validation; not for agent tool loops |
| **langchain** | Agent framework | Yes (via integrations) | Yes (via bind_tools) | Yes | Partial | Heavy; complex; overkill for just LLM abstraction |
| **openai SDK** | OpenAI API | OpenAI only | Yes (native) | Yes | Yes (response_format) | Direct; no translation needed for OpenAI |
| **anthropic SDK** | Anthropic API | Anthropic only | Yes (native) | Yes | No | Direct; no translation needed for Anthropic |
| **google-genai** | Gemini API | Google only | Yes (native) | Yes | Partial | Direct; no translation needed for Gemini |

---

### Related Specs

- `.trellis/spec/backend/local-worker-bridge-spec.md` — Worker bridge spec (may need updates for multi-provider)
- `.trellis/spec/backend/nats-bridge-spec.md` — NATS bridge (provider-agnostic)

---

## Caveats / Not Found

1. **Cursor's architecture**: Cursor is closed-source; its multi-provider implementation is not publicly available. Based on observable behavior, it likely uses a similar abstract base class pattern with per-provider adapters.

2. **OpenHands LLM module**: The exact file path for OpenHands' LLM abstraction could not be fetched (404 errors on GitHub raw URLs). The analysis is based on directory structure and litellm integration evidence.

3. **Streaming normalization**: The detailed streaming chunk normalization across providers was not fully examined. This is a significant implementation detail that would need careful handling (Anthropic uses `event: content_block_delta`, OpenAI uses `data: {"choices": [...]}`, Gemini uses different SSE format).

4. **Provider-specific features not covered**: Anthropic's prompt caching, OpenAI's response API, Gemini's grounding/search, and other provider-specific features would need additional research for a full implementation plan.

5. **litellm version**: The research is based on the current main branch of litellm. API stability and version compatibility should be verified before adoption.

6. **Cost of litellm dependency**: litellm has a large dependency tree (includes `openai`, `tiktoken`, `httpx`, `pydantic`, and many provider-specific SDKs). The lazy-loading pattern from Aider mitigates the import time cost but not the disk/installation cost.
