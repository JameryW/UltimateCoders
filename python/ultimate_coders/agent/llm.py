"""LLM client abstraction for agent interactions.

Supports tool calling for Worker execution and structured output
for Orchestrator decomposition. Defaults to the Anthropic API.
Multi-provider support via litellm delegation (Aider/OpenHands pattern).
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ponytail: transient upstream errors worth a backoff retry.
# 503/server_error/"system is busy"/"try again later" are how LLM proxies and
# gateways signal temporary overload — distinct from 429 (rate limit) and 529
# (Anthropic overloaded), but equally retriable. Without this, a single 503
# fails the whole subtask instead of backing off.
_TRANSIENT_RETRY_MARKERS = (
    "429",
    "rate_limit",
    "529",
    "overloaded",
    "503",
    "server_error",
    "system is busy",
    "try again later",
    "service unavailable",
)


def _is_transient_api_error(error_str: str) -> bool:
    """Whether an LLM API error string looks transient (retryable with backoff)."""
    low = error_str.lower()
    return any(m in low for m in _TRANSIENT_RETRY_MARKERS)


# ponytail: permanent error markers — 400/401/403/404 etc. are NOT retryable.
# Kept as a separate tuple from _TRANSIENT_RETRY_MARKERS so callers can classify
# an error as transient, permanent, or unknown without calling the LLM provider.
_PERMANENT_ERROR_MARKERS = (
    "400",
    "401",
    "403",
    "404",
    "invalid_api_key",
    "invalid key",
    "authentication",
    "unauthorized",
    "forbidden",
    "not found",
    "bad request",
    "invalid request",
)


@dataclass
class LLMErrorClassification:
    """Structured classification of an LLM API error.

    kind: "transient" (retryable), "permanent" (not retryable), or "unknown".
    retry_count: how many retries were attempted before this error surfaced.
    message: the original error string (root cause).
    """

    kind: str  # "transient" | "permanent" | "unknown"
    retry_count: int
    message: str


def _classify_llm_error(error: Any, retry_count: int = 0) -> LLMErrorClassification:
    """Classify an LLM API error as transient, permanent, or unknown.

    Reuses _is_transient_api_error for transient detection, then checks
    _PERMANENT_ERROR_MARKERS for permanent errors. Falls back to "unknown".

    Args:
        error: The exception object (or string) raised by the LLM provider.
        retry_count: How many retries were already attempted.

    Returns:
        LLMErrorClassification with kind, retry_count, and the root-cause message.
    """
    error_str = str(error)
    if _is_transient_api_error(error_str):
        kind = "transient"
    elif any(m in error_str.lower() for m in _PERMANENT_ERROR_MARKERS):
        kind = "permanent"
    else:
        kind = "unknown"
    return LLMErrorClassification(kind=kind, retry_count=retry_count, message=error_str)


class LLMRetryExhaustedError(RuntimeError):
    """Raised when an LLM API call exhausts all retries.

    Carries the original exception and the classification so the Worker can
    build a friendly error message without re-parsing the provider string.
    """

    def __init__(self, original: Exception, classification: LLMErrorClassification) -> None:
        self.original = original
        self.classification = classification
        super().__init__(str(original))

# Provider-specific API key env var mapping
_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# ponytail: env vars for default model per provider (proxy deployments often use custom model names)
_PROVIDER_MODEL_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "openai": "OPENAI_DEFAULT_MODEL",
    "gemini": "GEMINI_DEFAULT_MODEL",
    "deepseek": "DEEPSEEK_DEFAULT_MODEL",
}


@dataclass
class ToolDefinition:
    """Definition of a tool available for LLM function calling."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """A tool call requested by the LLM."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM completion."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class GenericStreamingChunk:
    """A single chunk from a streaming LLM response.

    Normalizes streaming output across providers (Anthropic native + litellm).
    Each chunk is a delta — callers accumulate to build the full response.
    """

    # ponytail: minimal chunk type, add fields when needed
    text_delta: str = ""
    tool_call_delta: ToolCall | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient:
    """Abstract LLM client for agent interactions.

    Supports tool calling for Worker execution and structured output
    for Orchestrator decomposition.

    Usage:
        client = LLMClient(api_key="...", model="claude-sonnet-4-6")
        response = await client.complete(messages=[...])
        response = await client.complete_with_tools(messages, tools)
    """

    def __init__(
        self,
        provider: str = "anthropic",
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 5,
        rpm_limit: int = 60,
        tpm_limit: int = 100000,
    ):
        self.provider = provider
        # Resolve API key: explicit > provider env > ANTHROPIC_API_KEY > ANTHROPIC_AUTH_TOKEN
        env_key = _PROVIDER_KEY_ENV.get(provider, "ANTHROPIC_API_KEY")
        self.api_key = (
            api_key
            or os.environ.get(env_key)
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        # ponytail: model defaults per provider, env override takes precedence
        env_model_key = _PROVIDER_MODEL_ENV.get(provider)
        default_models: dict[str, str] = {
            "anthropic": "claude-sonnet-4-6",
            "openai": "gpt-4o",
            "gemini": "gemini-2.5-pro",
            "deepseek": "deepseek/deepseek-chat",
        }
        env_model = os.environ.get(env_model_key) if env_model_key else None
        self.model = model or env_model or default_models.get(provider, "claude-sonnet-4-6")
        self.max_retries = max_retries
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazily initialize the LLM client.

        For provider='anthropic', uses the native Anthropic SDK (stable, zero-change).
        For any other provider, delegates to litellm (Aider/OpenHands pattern).
        """
        if self._client is not None:
            return self._client

        if self.provider == "anthropic":
            try:
                import anthropic

                kwargs: dict[str, Any] = {}
                if self.api_key:
                    kwargs["api_key"] = self.api_key
                self._client = anthropic.AsyncAnthropic(**kwargs)
            except ImportError:
                raise ImportError(
                    "The 'anthropic' package is required for LLM integration. "
                    "Install it with: pip install anthropic"
                )
        else:
            # litellm delegation — lazy import to avoid ~1.5s startup cost
            try:
                import litellm

                self._client = litellm
            except ImportError:
                raise ImportError(
                    f"The 'litellm' package is required for provider '{self.provider}'. "
                    "Install it with: pip install litellm"
                )
        return self._client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a completion request to the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            system: Optional system prompt.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature.
            **kwargs: Additional parameters for the API.

        Returns:
            LLMResponse with text content.
        """
        if self.provider == "anthropic":
            return await self._complete_anthropic(
                messages, system=system, max_tokens=max_tokens,
                temperature=temperature, **kwargs,
            )
        return await self._complete_litellm(
            messages, system=system, max_tokens=max_tokens,
            temperature=temperature, **kwargs,
        )

    async def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """Anthropic-native completion path (zero-change from original)."""
        client = self._get_client()

        request_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            request_params["system"] = system
        request_params.update(kwargs)

        response = await self._call_with_retry(client, request_params)
        return self._parse_anthropic_response(response)

    async def _complete_litellm(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """litellm completion path — OpenAI-format I/O, provider auto-routing."""
        client = self._get_client()

        # Inject system as a message (OpenAI convention)
        openai_messages = list(messages)
        if system:
            openai_messages.insert(0, {"role": "system", "content": system})

        request_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": openai_messages,
        }
        if self.api_key:
            request_params["api_key"] = self.api_key
        request_params.update(kwargs)

        response = await self._call_litellm_with_retry(client, request_params)
        return self._parse_litellm_response(response)

    async def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        system: str | None = None,
        max_tokens: int = 4096,
        max_tool_rounds: int = 20,
        tool_executor: Any | None = None,
        on_tool_call: Any | None = None,
        **kwargs: Any,
    ) -> tuple[LLMResponse, list[dict[str, Any]]]:
        """Complete with tool calling loop.

        Executes the standard tool-calling loop:
        1. Send messages + tools to LLM
        2. If LLM requests tool calls, execute them and append results
        3. Repeat until LLM stops requesting tools or max rounds reached

        Args:
            messages: Conversation messages.
            tools: Available tool definitions.
            system: Optional system prompt.
            max_tokens: Maximum tokens per response.
            max_tool_rounds: Maximum tool-calling iterations.
            tool_executor: Callable that takes a ToolCall and returns a string result.
                           If None, tool calls are not executed.
            on_tool_call: Optional async callback invoked after each tool call
                          with (tool_name, tool_input, result). Used by the
                          Dashboard event emitter to stream real-time interactions.
            **kwargs: Additional parameters for the API.

        Returns:
            Tuple of (final LLMResponse, tool_calls_log) where tool_calls_log
            is a list of dicts with 'tool_call' and 'result' entries.
        """
        tool_calls_log: list[dict[str, Any]] = []
        working_messages = list(messages)

        if self.provider == "anthropic":
            return await self._complete_with_tools_anthropic(
                working_messages, tools, system=system, max_tokens=max_tokens,
                max_tool_rounds=max_tool_rounds, tool_executor=tool_executor,
                on_tool_call=on_tool_call, tool_calls_log=tool_calls_log, **kwargs,
            )
        return await self._complete_with_tools_litellm(
            working_messages, tools, system=system, max_tokens=max_tokens,
            max_tool_rounds=max_tool_rounds, tool_executor=tool_executor,
            on_tool_call=on_tool_call, tool_calls_log=tool_calls_log, **kwargs,
        )

    async def complete_stream(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        """Stream a completion response, yielding GenericStreamingChunk deltas.

        Args:
            messages: Conversation messages.
            system: Optional system prompt.
            max_tokens: Maximum tokens in the response.
            **kwargs: Additional parameters for the API.

        Yields:
            GenericStreamingChunk objects with text_delta, finish_reason, usage.
        """

        if self.provider == "anthropic":
            async for chunk in self._stream_anthropic(
                messages, system=system, max_tokens=max_tokens, **kwargs,
            ):
                yield chunk
        else:
            async for chunk in self._stream_litellm(
                messages, system=system, max_tokens=max_tokens, **kwargs,
            ):
                yield chunk

    async def _stream_anthropic(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        """Anthropic-native streaming path."""

        client = self._get_client()
        request_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            request_params["system"] = system
        request_params.update(kwargs)

        async with client.messages.stream(**request_params) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield GenericStreamingChunk(text_delta=event.delta.text)
                elif event.type == "message_stop":
                    yield GenericStreamingChunk(finish_reason="end_turn")
                elif event.type == "message_delta":
                    # Final delta with usage
                    usage = {}
                    if hasattr(event, "usage"):
                        usage = {
                            "output_tokens": getattr(event.usage, "output_tokens", 0),
                        }
                    stop_reason = getattr(event.delta, "stop_reason", "end_turn") or "end_turn"
                    yield GenericStreamingChunk(finish_reason=stop_reason, usage=usage)

    async def _stream_litellm(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        """litellm streaming path — OpenAI-format SSE."""

        client = self._get_client()
        openai_messages = list(messages)
        if system:
            openai_messages.insert(0, {"role": "system", "content": system})

        request_params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
            "stream": True,
        }
        if self.api_key:
            request_params["api_key"] = self.api_key
        request_params.update(kwargs)

        response = await client.acompletion(**request_params)
        async for chunk in response:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta
            text_delta = ""
            if hasattr(delta, "content") and delta.content:
                text_delta = delta.content
            finish_reason = getattr(choice, "finish_reason", None)
            usage = {}
            if hasattr(chunk, "usage") and chunk.usage:
                usage = {
                    "input_tokens": getattr(chunk.usage, "prompt_tokens", 0),
                    "output_tokens": getattr(chunk.usage, "completion_tokens", 0),
                }
            yield GenericStreamingChunk(
                text_delta=text_delta,
                finish_reason=finish_reason,
                usage=usage,
            )

    async def _complete_with_tools_anthropic(
        self,
        working_messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        system: str | None = None,
        max_tokens: int = 4096,
        max_tool_rounds: int = 20,
        tool_executor: Any | None = None,
        on_tool_call: Any | None = None,
        tool_calls_log: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> tuple[LLMResponse, list[dict[str, Any]]]:
        """Anthropic-native tool-calling loop (zero-change from original)."""
        if tool_calls_log is None:
            tool_calls_log = []
        anthropic_tools = [self._format_tool_anthropic(t) for t in tools]
        llm_response = LLMResponse()

        for _ in range(max_tool_rounds):
            client = self._get_client()

            request_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": working_messages,
                "tools": anthropic_tools,
            }
            if system:
                request_params["system"] = system
            request_params.update(kwargs)

            response = await self._call_with_retry(client, request_params)
            llm_response = self._parse_anthropic_response(response)

            if not llm_response.has_tool_calls:
                return llm_response, tool_calls_log

            # Process tool calls. Anthropic expects ALL tool_use blocks for
            # one assistant turn in a SINGLE assistant message, with ALL
            # tool_results in a SINGLE following user message. Build the
            # blocks across the loop, then append once after.
            tool_use_blocks = []
            tool_result_blocks = []
            for tool_call in llm_response.tool_calls:
                tool_result_str = ""
                if tool_executor is not None:
                    try:
                        tool_result_str = await tool_executor(tool_call)
                    except Exception as e:
                        tool_result_str = f"Error executing tool {tool_call.name}: {e}"
                        logger.error("Tool execution error: %s", e)

                tool_calls_log.append(
                    {
                        "tool_call": {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "input": tool_call.input,
                        },
                        "result": tool_result_str,
                    }
                )

                if on_tool_call is not None:
                    try:
                        await on_tool_call(tool_call.name, tool_call.input, tool_result_str)
                    except Exception:
                        logger.debug("on_tool_call callback error", exc_info=True)

                tool_use_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.input,
                    }
                )
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": tool_result_str,
                    }
                )

            working_messages.append({"role": "assistant", "content": tool_use_blocks})
            working_messages.append({"role": "user", "content": tool_result_blocks})

        # Exhausted max_tool_rounds while the LLM still requests tools — no
        # final text was synthesized. Mark the stop_reason so callers can
        # detect this (the response has has_tool_calls=True, text likely "").
        llm_response.stop_reason = "max_tool_rounds"
        return llm_response, tool_calls_log

    async def _complete_with_tools_litellm(
        self,
        working_messages: list[dict[str, Any]],
        tools: list[ToolDefinition],
        system: str | None = None,
        max_tokens: int = 4096,
        max_tool_rounds: int = 20,
        tool_executor: Any | None = None,
        on_tool_call: Any | None = None,
        tool_calls_log: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> tuple[LLMResponse, list[dict[str, Any]]]:
        """litellm tool-calling loop — OpenAI-format I/O, provider auto-routing."""
        if tool_calls_log is None:
            tool_calls_log = []
        openai_tools = [self._format_tool_openai(t) for t in tools]

        # Inject system message if provided
        if system and (not working_messages or working_messages[0].get("role") != "system"):
            working_messages.insert(0, {"role": "system", "content": system})

        llm_response = LLMResponse()

        for _ in range(max_tool_rounds):
            client = self._get_client()

            request_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": working_messages,
                "tools": openai_tools,
            }
            if self.api_key:
                request_params["api_key"] = self.api_key
            request_params.update(kwargs)

            response = await self._call_litellm_with_retry(client, request_params)
            llm_response = self._parse_litellm_response(response)

            if not llm_response.has_tool_calls:
                return llm_response, tool_calls_log

            # Process tool calls in OpenAI format
            assistant_msg = {"role": "assistant", "content": llm_response.text or None}
            if llm_response.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc in llm_response.tool_calls
                ]
            working_messages.append(assistant_msg)

            for tool_call in llm_response.tool_calls:
                tool_result_str = ""
                if tool_executor is not None:
                    try:
                        tool_result_str = await tool_executor(tool_call)
                    except Exception as e:
                        tool_result_str = f"Error executing tool {tool_call.name}: {e}"
                        logger.error("Tool execution error: %s", e)

                tool_calls_log.append(
                    {
                        "tool_call": {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "input": tool_call.input,
                        },
                        "result": tool_result_str,
                    }
                )

                if on_tool_call is not None:
                    try:
                        await on_tool_call(tool_call.name, tool_call.input, tool_result_str)
                    except Exception:
                        logger.debug("on_tool_call callback error", exc_info=True)

                # OpenAI tool result format
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result_str,
                    }
                )

        # Exhausted max_tool_rounds while the LLM still requests tools.
        llm_response.stop_reason = "max_tool_rounds"
        return llm_response, tool_calls_log

    async def _call_with_retry(self, client: Any, params: dict[str, Any]) -> Any:
        """Call the Anthropic API with exponential backoff and jitter retry."""
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(self.max_retries):
            try:
                return await client.messages.create(**params)
            except Exception as e:
                error_str = str(e)

                if not _is_transient_api_error(error_str):
                    # Permanent error — classify and wrap so Worker can read kind.
                    raise LLMRetryExhaustedError(
                        e, _classify_llm_error(e, 0),
                    ) from e

                if attempt >= self.max_retries - 1:
                    # Transient error, retries exhausted — wrap with retry count.
                    raise LLMRetryExhaustedError(
                        e, _classify_llm_error(e, self.max_retries),
                    ) from e

                # Exponential backoff with jitter
                exp_delay = base_delay * (2**attempt)
                jitter = random.uniform(0, 0.5)  # noqa: S311
                delay = min(exp_delay + jitter, max_delay)
                logger.warning(
                    "LLM API transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    error_str,
                )
                import asyncio

                await asyncio.sleep(delay)

        raise RuntimeError("Unreachable: max retries exceeded")

    async def _call_litellm_with_retry(self, litellm_mod: Any, params: dict[str, Any]) -> Any:
        """Call litellm.acompletion() with exponential backoff and jitter retry."""
        import asyncio

        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(self.max_retries):
            try:
                return await litellm_mod.acompletion(**params)
            except Exception as e:
                error_str = str(e)

                if not _is_transient_api_error(error_str):
                    raise LLMRetryExhaustedError(
                        e, _classify_llm_error(e, 0),
                    ) from e

                if attempt >= self.max_retries - 1:
                    raise LLMRetryExhaustedError(
                        e, _classify_llm_error(e, self.max_retries),
                    ) from e

                exp_delay = base_delay * (2**attempt)
                jitter = random.uniform(0, 0.5)  # noqa: S311
                delay = min(exp_delay + jitter, max_delay)
                logger.warning(
                    "litellm transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    self.max_retries,
                    delay,
                    error_str,
                )
                await asyncio.sleep(delay)

        raise RuntimeError("Unreachable: max retries exceeded")

    def _parse_anthropic_response(self, response: Any) -> LLMResponse:
        """Parse an Anthropic API response into LLMResponse."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=block.input if isinstance(block.input, dict) else {},
                    )
                )

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0),
                "output_tokens": getattr(response.usage, "output_tokens", 0),
            }

        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=getattr(response, "stop_reason", "end_turn") or "end_turn",
            model=getattr(response, "model", self.model) or self.model,
            usage=usage,
        )

    def _parse_litellm_response(self, response: Any) -> LLMResponse:
        """Parse a litellm ModelResponse (OpenAI-format) into LLMResponse."""
        if not getattr(response, "choices", None):
            # Some providers return empty choices on content-filter or error.
            # Surface as an empty response rather than IndexError.
            return LLMResponse(
                text="",
                tool_calls=[],
                stop_reason="empty_choices",
                model=getattr(response, "model", self.model) or self.model,
                usage={},
            )
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                # OpenAI format: tc.function.name, tc.function.arguments (JSON string)
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, input=args)
                )

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": getattr(response.usage, "prompt_tokens", 0),
                "output_tokens": getattr(response.usage, "completion_tokens", 0),
            }

        # ponytail: finish_reason maps to stop_reason; "tool_calls" → "tool_use"
        stop_reason = getattr(choice, "finish_reason", "stop") or "stop"
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            model=getattr(response, "model", self.model) or self.model,
            usage=usage,
        )

    @staticmethod
    def _format_tool_anthropic(tool: ToolDefinition) -> dict[str, Any]:
        """Format a ToolDefinition for the Anthropic API."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema
            or {
                "type": "object",
                "properties": {},
            },
        }

    @staticmethod
    def _format_tool_openai(tool: ToolDefinition) -> dict[str, Any]:
        """Format a ToolDefinition for OpenAI/litellm API."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema
                or {
                    "type": "object",
                    "properties": {},
                },
            },
        }


def make_tool_definition(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> ToolDefinition:
    """Helper to build a ToolDefinition with a JSON Schema input_schema.

    Args:
        name: Tool name.
        description: What the tool does.
        parameters: JSON Schema properties dict. Each key is a param name,
                    value is a dict with 'type', 'description', and optional
                    'required' (bool), 'enum' (list), 'items' (dict for arrays).

    Returns:
        ToolDefinition with properly structured input_schema.
    """
    properties = parameters or {}
    # Separate 'required' flag from schema properties
    required_keys = [k for k, v in properties.items() if v.get("required", False)]
    # Build clean properties (strip internal 'required' marker)
    clean_props: dict[str, Any] = {}
    for key, val in properties.items():
        prop = {k: v for k, v in val.items() if k != "required"}
        clean_props[key] = prop
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": clean_props,
            "required": required_keys,
        },
    )
