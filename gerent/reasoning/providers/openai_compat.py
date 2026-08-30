"""OpenAI-compatible adapters: OpenAI, OpenRouter, and Ollama.

One wire format, three configurations - they differ in base URL, auth, model-id
namespace, and which capabilities are real. Splitting them into three copied adapters
would triple the surface where streamed tool-call fragments get reassembled wrong.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from gerent.core.errors import (
    FatalProviderError,
    ProviderUnavailable,
    RetryableProviderError,
)
from gerent.core.types import (
    Msg,
    Role,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)
from gerent.reasoning.providers.base import (
    Capabilities,
    CompletionRequest,
    ModelProvider,
    ProviderEvent,
    Stop,
    StopReason,
    TextDelta,
    ToolCallComplete,
    UsageReport,
)

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAICompatProvider(ModelProvider):
    """Shared implementation. Subclasses set name, defaults, and capabilities."""

    name = "openai-compat"
    default_base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    requires_key: bool = True
    prices: dict[str, tuple[float, float]] = {}

    capabilities = Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        thinking=False,
        effort_levels=(),
        prompt_caching="automatic",
        deferred_tools=False,
        server_compaction=False,
        task_budget=False,
        vision=True,
        json_schema_output=True,
        max_context=128_000,
    )

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise ProviderUnavailable(
                "openai SDK not installed - pip install 'gerent[openai]'", provider=self.name
            ) from exc
        key = api_key or os.environ.get(self.api_key_env)
        if not key and not self.requires_key:
            key = "none"
        if self.requires_key and not key:
            raise ProviderUnavailable(f"{self.api_key_env} is not set", provider=self.name)
        self._client = AsyncOpenAI(
            api_key=key or "none", base_url=base_url or self.default_base_url
        )

    @classmethod
    def available(cls) -> tuple[bool, str]:
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "openai SDK not installed"
        if cls.requires_key and not os.environ.get(cls.api_key_env):
            return False, f"{cls.api_key_env} is not set"
        return True, ""

    # ── rendering ────────────────────────────────────────────────────────────

    def _render_messages(self, messages: list[Msg]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role is Role.SYSTEM:
                out.append({"role": "system", "content": msg.text()})
                continue

            if msg.role is Role.TOOL:
                # Each result is its own message here, unlike Anthropic's single
                # user turn carrying every tool_result block.
                for block in msg.blocks:
                    if isinstance(block, ToolResultBlock):
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.tool_call_id,
                                "content": block.content,
                            }
                        )
                continue

            if msg.role is Role.USER:
                out.append({"role": "user", "content": msg.text()})
                continue

            entry: dict[str, Any] = {"role": "assistant", "content": msg.text() or None}
            calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {"name": b.name, "arguments": json.dumps(b.arguments)},
                }
                for b in msg.blocks
                if isinstance(b, ToolCallBlock)
            ]
            if calls:
                entry["tool_calls"] = calls
            if entry["content"] is None and not calls:
                continue
            out.append(entry)
        return out

    def _render_tools(self, req: CompletionRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in req.tools
        ]

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream(self, req: CompletionRequest) -> AsyncIterator[ProviderEvent]:
        import openai

        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": self._render_messages(req.messages),
            "max_completion_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.system:
            kwargs["messages"] = [{"role": "system", "content": req.system}] + kwargs["messages"]
        if tools := self._render_tools(req):
            kwargs["tools"] = tools
        if self.capabilities.supports_effort(req.effort):
            kwargs["reasoning_effort"] = req.effort
        elif req.effort:
            self.note_degradation(req, f"{self.name}: effort {req.effort!r} unsupported, ignored")
        if req.task_budget_tokens:
            self.note_degradation(req, f"{self.name}: task budget unsupported, tracked client-side")

        # Tool-call arguments stream as JSON fragments keyed by index. They must be
        # accumulated before parsing; parsing a fragment yields nonsense, and matching
        # on the string instead of parsing breaks on vendor-specific escaping.
        partial: dict[int, dict[str, str]] = {}
        finish = "stop"
        usage = Usage()

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = self._usage(req.model, chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if getattr(delta, "content", None):
                    yield TextDelta(delta.content)
                for call in getattr(delta, "tool_calls", None) or []:
                    slot = partial.setdefault(call.index, {"id": "", "name": "", "args": ""})
                    if call.id:
                        slot["id"] = call.id
                    if call.function and call.function.name:
                        slot["name"] = call.function.name
                    if call.function and call.function.arguments:
                        slot["args"] += call.function.arguments
        except openai.APIStatusError as exc:
            raise self._map_status_error(exc) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise RetryableProviderError(str(exc), provider=self.name) from exc

        for slot in partial.values():
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                # A model that emits malformed arguments is a bad turn, not a crash:
                # surface it as an empty call and let the tool result say so.
                arguments = {}
            yield ToolCallComplete(
                id=slot["id"] or slot["name"], name=slot["name"], arguments=arguments
            )

        yield UsageReport(usage)
        yield Stop(_STOP_REASONS.get(finish, StopReason.END_TURN))

    def _usage(self, model: str, u: Any) -> Usage:
        inp = getattr(u, "prompt_tokens", 0) or 0
        out = getattr(u, "completion_tokens", 0) or 0
        cached = 0
        if details := getattr(u, "prompt_tokens_details", None):
            cached = getattr(details, "cached_tokens", 0) or 0
        in_price, out_price = self.prices.get(model, (0.0, 0.0))
        return Usage(
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cached,
            cost_usd=(inp * in_price + out * out_price) / 1_000_000,
        )

    def _map_status_error(self, exc: Any) -> Exception:
        status = getattr(exc, "status_code", None)
        if status == 404:
            # Rosters change without notice, especially on gateways. This is a config
            # error with a clear message, not something to fail over on.
            return FatalProviderError(
                f"model not found on {self.name}: {exc}", provider=self.name, status=404
            )
        if status in (408, 409, 429) or (status or 0) >= 500:
            return RetryableProviderError(str(exc), provider=self.name, status=status)
        return FatalProviderError(str(exc), provider=self.name, status=status)


class OpenAIProvider(OpenAICompatProvider):
    name = "openai"
    api_key_env = "OPENAI_API_KEY"
    capabilities = Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        thinking=True,
        effort_levels=("low", "medium", "high"),
        prompt_caching="automatic",
        vision=True,
        json_schema_output=True,
        max_context=400_000,
    )


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"
    capabilities = Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        # A gateway fronting many models: capabilities are a floor, not a promise.
        # Anything model-specific is negotiated down to what every route supports.
        thinking=False,
        effort_levels=(),
        prompt_caching="none",
        vision=True,
        json_schema_output=False,
        max_context=128_000,
    )


class OllamaProvider(OpenAICompatProvider):
    name = "ollama"
    default_base_url = "http://localhost:11434/v1"
    requires_key = False
    capabilities = Capabilities(
        tools=True,
        # Tool-calling support varies sharply by local model and is the most common
        # failure. The conformance suite is what says whether a given one is usable.
        parallel_tool_calls=False,
        streaming=True,
        thinking=False,
        effort_levels=(),
        prompt_caching="none",
        vision=False,
        json_schema_output=False,
        max_context=32_000,
    )
