"""One model call: stream it, accumulate it into a normalized Msg, account for it.

The loop around this lives in the kernel. Keeping them separate means the loop is
testable without a provider and the accumulation is testable without a loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from gerent.core.types import (
    EventKind,
    Msg,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    TurnEvent,
    Usage,
)
from gerent.reasoning.providers.base import (
    CompletionRequest,
    Stop,
    StopReason,
    TextDelta,
    ThinkingDelta,
    ToolCallComplete,
    ToolSpec,
    UsageReport,
)
from gerent.reasoning.router import Router
from gerent.reporting.journal import Journal, JournalKind

log = structlog.get_logger(__name__)

Emit = Callable[[TurnEvent], Awaitable[None]]


@dataclass
class Completion:
    message: Msg
    stop: StopReason
    detail: str = ""
    usage: Usage = None  # type: ignore[assignment]
    degradations: list[str] = None  # type: ignore[assignment]


class Engine:
    def __init__(self, router: Router, *, max_tokens: int = 16_000) -> None:
        self.router = router
        self.max_tokens = max_tokens

    async def call(
        self,
        role: str,
        messages: list[Msg],
        *,
        system: str = "",
        tools: list[ToolSpec] | None = None,
        journal: Journal | None = None,
        emit: Emit | None = None,
        task_budget_tokens: int | None = None,
    ) -> Completion:
        req = CompletionRequest(
            model="",
            # A snapshot, not the caller's live list: the kernel appends to its history
            # as the turn proceeds, and a request that mutates after it was sent is a
            # trap for any adapter that renders lazily.
            messages=list(messages),
            system=system,
            tools=tools or [],
            max_tokens=self.max_tokens,
            task_budget_tokens=task_budget_tokens,
        )

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCallBlock] = []
        usage = Usage()
        stop = StopReason.END_TURN
        detail = ""

        async for event in self.router.stream(role, req):
            match event:
                case TextDelta(text=text):
                    text_parts.append(text)
                    if emit:
                        await emit(TurnEvent.text_delta(text))
                case ThinkingDelta(text=text):
                    thinking_parts.append(text)
                    if emit:
                        await emit(TurnEvent.thinking(text))
                case ToolCallComplete(id=cid, name=name, arguments=args):
                    calls.append(ToolCallBlock(id=cid, name=name, arguments=args))
                case UsageReport(usage=reported):
                    usage = reported
                case Stop(reason=reason, detail=stop_detail):
                    stop, detail = reason, stop_detail

        blocks: list[TextBlock | ThinkingBlock | ToolCallBlock] = []
        if thinking := "".join(thinking_parts):
            blocks.append(ThinkingBlock(text=thinking))
        if text := "".join(text_parts):
            blocks.append(TextBlock(text=text))
        blocks.extend(calls)

        if journal is not None:
            journal.add_usage(usage)
            journal.append(
                JournalKind.MODEL_CALL,
                role=role,
                model=req.model,
                stop=str(stop),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cost_usd=round(usage.cost_usd, 6),
            )
            for note in req.degradations:
                journal.append(JournalKind.DEGRADATION, detail=note)

        if emit:
            for note in req.degradations:
                await emit(TurnEvent(kind=EventKind.DEGRADATION, text=note))

        return Completion(
            message=Msg(role=Role.ASSISTANT, blocks=blocks),  # type: ignore[arg-type]
            stop=stop,
            detail=detail,
            usage=usage,
            degradations=list(req.degradations),
        )
