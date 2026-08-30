"""A scriptable provider for tests. No network, no spend.

Integration tests drive the whole kernel through this, so the loop, guardrails, and
reporting are all exercised without an API key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from gerent.core.errors import RetryableProviderError
from gerent.core.types import Usage
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


class FakeProvider(ModelProvider):
    name = "fake"
    capabilities = Capabilities(
        tools=True,
        parallel_tool_calls=True,
        streaming=True,
        effort_levels=("low", "high"),
        max_context=100_000,
    )

    def __init__(
        self,
        turns: Iterable[list[ProviderEvent]] | None = None,
        *,
        fail_times: int = 0,
        **_: object,
    ) -> None:
        self._turns = list(turns or [])
        self._fail_times = fail_times
        self.calls: list[CompletionRequest] = []

    def script(self, *events: ProviderEvent) -> FakeProvider:
        self._turns.append(list(events))
        return self

    def say(self, text: str) -> FakeProvider:
        return self.script(TextDelta(text), UsageReport(Usage(input_tokens=10, output_tokens=5)), Stop(StopReason.END_TURN))

    def call_tool(self, name: str, **arguments: object) -> FakeProvider:
        return self.script(
            ToolCallComplete(id=f"call_{name}", name=name, arguments=dict(arguments)),
            UsageReport(Usage(input_tokens=10, output_tokens=5)),
            Stop(StopReason.TOOL_USE),
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[ProviderEvent]:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RetryableProviderError("scripted failure", provider=self.name, status=503)
        self.calls.append(req)
        events = self._turns.pop(0) if self._turns else [
            TextDelta("(no script)"),
            UsageReport(Usage()),
            Stop(StopReason.END_TURN),
        ]
        for event in events:
            yield event
