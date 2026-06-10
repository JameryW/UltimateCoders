"""LLM client abstraction for agent interactions.

Supports tool calling for Worker execution and structured output
for Orchestrator decomposition. Defaults to the Anthropic API.
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    """Definition of a tool available for LLM function calling."""
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """A tool call requested by the LLM."""
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM completion."""
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


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
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 5,
        rpm_limit: int = 60,
        tpm_limit: int = 100000,
    ):
        self.provider = provider
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or "claude-sonnet-4-6"
        self.max_retries = max_retries
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self._client: Optional[Any] = None

    def _get_client(self) -> Any:
        """Lazily initialize the Anthropic client."""
        if self._client is not None:
            return self._client

        if self.provider != "anthropic":
            raise ValueError(
                f"Unsupported LLM provider: {self.provider}. "
                "Only 'anthropic' is supported in the MVP."
            )

        try:
            import anthropic
            kwargs: Dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = anthropic.AsyncAnthropic(**kwargs)
        except ImportError:
            raise ImportError(
                "The 'anthropic' package is required for LLM integration. "
                "Install it with: pip install anthropic"
            )
        return self._client

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
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
        client = self._get_client()

        request_params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            request_params["system"] = system
        request_params.update(kwargs)

        response = await self._call_with_retry(client, request_params)
        return self._parse_response(response)

    async def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[ToolDefinition],
        system: Optional[str] = None,
        max_tokens: int = 4096,
        max_tool_rounds: int = 20,
        tool_executor: Optional[Any] = None,
        **kwargs: Any,
    ) -> Tuple[LLMResponse, List[Dict[str, Any]]]:
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
            **kwargs: Additional parameters for the API.

        Returns:
            Tuple of (final LLMResponse, tool_calls_log) where tool_calls_log
            is a list of dicts with 'tool_call' and 'result' entries.
        """
        tool_calls_log: List[Dict[str, Any]] = []
        working_messages = list(messages)

        # Format tools for Anthropic API
        anthropic_tools = [self._format_tool(t) for t in tools]

        for _ in range(max_tool_rounds):
            client = self._get_client()

            request_params: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": working_messages,
                "tools": anthropic_tools,
            }
            if system:
                request_params["system"] = system
            request_params.update(kwargs)

            response = await self._call_with_retry(client, request_params)
            llm_response = self._parse_response(response)

            if not llm_response.has_tool_calls:
                return llm_response, tool_calls_log

            # Process tool calls
            tool_use_blocks = []
            for tool_call in llm_response.tool_calls:
                tool_result_str = ""
                if tool_executor is not None:
                    try:
                        tool_result_str = await tool_executor(tool_call)
                    except Exception as e:
                        tool_result_str = f"Error executing tool {tool_call.name}: {e}"
                        logger.error("Tool execution error: %s", e)

                tool_calls_log.append({
                    "tool_call": {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.input,
                    },
                    "result": tool_result_str,
                })

                tool_use_blocks.append({
                    "type": "tool_use",
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "input": tool_call.input,
                })

                # Append tool result message
                working_messages.append({"role": "assistant", "content": tool_use_blocks})
                working_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": tool_result_str,
                    }],
                })

        # Max rounds reached; return the last response
        return llm_response, tool_calls_log

    async def _call_with_retry(self, client: Any, params: Dict[str, Any]) -> Any:
        """Call the LLM API with exponential backoff and jitter retry."""
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(self.max_retries):
            try:
                return await client.messages.create(**params)
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "rate_limit" in error_str.lower()
                is_overloaded = "529" in error_str or "overloaded" in error_str.lower()

                if not (is_rate_limit or is_overloaded):
                    raise

                if attempt >= self.max_retries - 1:
                    raise

                # Exponential backoff with jitter
                exp_delay = base_delay * (2 ** attempt)
                jitter = random.uniform(0, 0.5)  # noqa: S311
                delay = min(exp_delay + jitter, max_delay)
                logger.warning(
                    "LLM API rate limited/overloaded (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    attempt + 1, self.max_retries, delay, error_str,
                )
                import asyncio
                await asyncio.sleep(delay)

        raise RuntimeError("Unreachable: max retries exceeded")

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse an Anthropic API response into LLMResponse."""
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input if isinstance(block.input, dict) else {},
                ))

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

    @staticmethod
    def _format_tool(tool: ToolDefinition) -> Dict[str, Any]:
        """Format a ToolDefinition for the Anthropic API."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema or {
                "type": "object",
                "properties": {},
            },
        }


def make_tool_definition(
    name: str,
    description: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> ToolDefinition:
    """Helper to build a ToolDefinition with a JSON Schema input_schema.

    Args:
        name: Tool name.
        description: What the tool does.
        parameters: JSON Schema properties dict. Each key is a param name,
                    value is a dict with 'type' and 'description'.

    Returns:
        ToolDefinition with properly structured input_schema.
    """
    properties = parameters or {}
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": [
                k for k, v in properties.items()
                if v.get("required", False)
            ],
        },
    )
