"""Anthropic adapter.

Details here are the ones docs/providers.md flags as easy to get wrong and expensive to
discover in production: adaptive thinking with summarized display, no budget_tokens or
sampling parameters, refusal checked before content is read, and prompt-cache
breakpoints placed so the stable prefix actually gets reused.
"""

from __future__ import annotations

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
    TextBlock,
    ThinkingBlock,
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
    ThinkingDelta,
    ToolCallComplete,
    UsageReport,
)

# USD per million tokens. Used for budget accounting; an unknown model costs nothing
# rather than crashing, and the report shows tokens regardless.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE,
    "stop_sequence": StopReason.END_TURN,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
}


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    capabilities = Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        thinking=True,
        effort_levels=("low", "medium", "high", "xhigh", "max"),
        prompt_caching="explicit",
        deferred_tools=True,
        server_compaction=True,
        task_budget=True,
        vision=True,
        json_schema_output=True,
        max_context=1_000_000,
    )

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise ProviderUnavailable(
                "anthropic SDK not installed - pip install 'gerent[anthropic]'",
                provider=self.name,
            ) from exc
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # A zero-arg client also resolves an `ant auth login` profile, so an unset
        # ANTHROPIC_API_KEY does not mean there are no credentials.
        self._client = AsyncAnthropic(**kwargs)

    @classmethod
    def available(cls) -> tuple[bool, str]:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK not installed"
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True, ""
        from pathlib import Path

        if (Path.home() / ".config/anthropic").exists():
            return True, ""
        return False, "no ANTHROPIC_API_KEY and no ant auth profile"

    # ── rendering ────────────────────────────────────────────────────────────

    def _render_messages(self, messages: list[Msg]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role is Role.SYSTEM:
                # Mid-conversation operator instruction: appended to messages rather
                # than the top-level system field, so the cached prefix survives.
                out.append({"role": "system", "content": msg.text()})
                continue

            content: list[dict[str, Any]] = []
            for block in msg.blocks:
                if isinstance(block, TextBlock):
                    if block.text:
                        content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolCallBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.arguments,
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_call_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )
                elif isinstance(block, ThinkingBlock):
                    # Not replayed. Replaying requires the original signature, and a
                    # summarized-display block does not carry one that round-trips.
                    # Dropping is safe; it costs a little continuity inside a turn.
                    continue

            if not content:
                continue
            # Anthropic carries tool results on a user turn.
            role = "user" if msg.role in (Role.USER, Role.TOOL) else "assistant"
            out.append({"role": role, "content": content})
        return out

    def _render_tools(self, req: CompletionRequest) -> list[dict[str, Any]]:
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in req.tools
        ]
        if tools:
            # Cache breakpoint after the (deterministically ordered) tool list, which
            # with the system prompt forms the stable prefix.
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream(self, req: CompletionRequest) -> AsyncIterator[ProviderEvent]:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": self._render_messages(req.messages),
            # Adaptive thinking, summarized. The default display is "omitted", which
            # would leave the voice port silent through a long think and the journal
            # with no reasoning recorded.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if req.system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": req.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if tools := self._render_tools(req):
            kwargs["tools"] = tools

        output_config: dict[str, Any] = {}
        if self.capabilities.supports_effort(req.effort):
            output_config["effort"] = req.effort
        if req.task_budget_tokens:
            # Advisory ceiling so a long unattended run paces itself and lands
            # gracefully, rather than being guillotined mid-task by max_tokens.
            output_config["task_budget"] = {
                "type": "tokens",
                "total": max(20_000, req.task_budget_tokens),
            }
            kwargs["betas"].append("task-budgets-2026-03-13")
        if output_config:
            kwargs["output_config"] = output_config

        try:
            async with self._client.beta.messages.stream(**kwargs) as stream:
                async for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "text":
                        yield TextDelta(event.text)
                    elif etype == "thinking":
                        yield ThinkingDelta(getattr(event, "thinking", ""))
                final = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise self._map_status_error(exc) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise RetryableProviderError(str(exc), provider=self.name) from exc

        # stop_reason is checked before content is read: a refusal returns HTTP 200,
        # and naive content[0].text access raises on it.
        reason = _STOP_REASONS.get(final.stop_reason or "end_turn", StopReason.END_TURN)

        if reason is not StopReason.REFUSAL:
            for block in final.content:
                if getattr(block, "type", "") == "tool_use":
                    yield ToolCallComplete(
                        id=block.id, name=block.name, arguments=dict(block.input or {})
                    )

        yield UsageReport(self._usage(req.model, final))
        detail = ""
        if reason is StopReason.REFUSAL:
            details = getattr(final, "stop_details", None)
            detail = getattr(details, "explanation", "") or "model declined the request"
        yield Stop(reason, detail)

    def _usage(self, model: str, final: Any) -> Usage:
        u = getattr(final, "usage", None)
        if u is None:
            return Usage()
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        in_price, out_price = PRICES.get(model, (0.0, 0.0))
        return Usage(
            input_tokens=inp,
            output_tokens=out,
            cache_read_tokens=cached,
            cost_usd=(inp * in_price + out * out_price) / 1_000_000,
        )

    def _map_status_error(self, exc: Any) -> Exception:
        status = getattr(exc, "status_code", None)
        if status in (408, 409, 429) or (status or 0) >= 500:
            return RetryableProviderError(str(exc), provider=self.name, status=status)
        return FatalProviderError(str(exc), provider=self.name, status=status)

    async def count_tokens(self, req: CompletionRequest) -> int:
        try:
            result = await self._client.messages.count_tokens(
                model=req.model,
                messages=self._render_messages(req.messages),
                system=req.system or "",
            )
            return int(result.input_tokens)
        except Exception:
            # Budget accounting should degrade to an estimate, never fail a run.
            return await super().count_tokens(req)
