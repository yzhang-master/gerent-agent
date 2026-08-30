"""The provider boundary.

This is the only layer permitted to import a vendor SDK. Everything above it speaks
Gerent's normalized types, which is what makes mid-conversation failover possible and
keeps the database schema portable.

See docs/providers.md and ADR 0001.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from gerent.core.types import Msg, Usage


class ToolSpec:
    """A tool as the provider layer sees it - rendered from a Skill, never hand-written.

    Kept deliberately plain: name, description, and a JSON Schema in the portable
    subset every provider accepts. See docs/skills.md#tool-rendering.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        defer_loading: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.defer_loading = defer_loading

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ToolSpec({self.name!r})"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    PAUSE = "pause"


# ── Streamed events ──────────────────────────────────────────────────────────
# Tool-call fragments are accumulated *inside* the adapter and emitted whole, so the
# engine never has to know that one vendor streams arguments as partial JSON strings
# keyed by index and another emits them complete.


@dataclass
class TextDelta:
    text: str


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ToolCallComplete:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class UsageReport:
    usage: Usage


@dataclass
class Stop:
    reason: StopReason
    detail: str = ""


ProviderEvent = TextDelta | ThinkingDelta | ToolCallComplete | UsageReport | Stop


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do.

    The kernel reads this and degrades explicitly. A missing capability must degrade,
    never crash - and every degradation taken is recorded in the run report, so a cheap
    run that silently lost tool search is diagnosable rather than mysterious.
    """

    tools: bool = True
    parallel_tool_calls: bool = True
    streaming: bool = True
    thinking: bool = False
    effort_levels: tuple[str, ...] = ()
    prompt_caching: Literal["none", "automatic", "explicit"] = "none"
    deferred_tools: bool = False
    server_compaction: bool = False
    task_budget: bool = False
    vision: bool = False
    json_schema_output: bool = False
    max_context: int = 128_000

    def supports_effort(self, effort: str | None) -> bool:
        return bool(effort) and effort in self.effort_levels


@dataclass
class CompletionRequest:
    model: str
    messages: list[Msg]
    system: str = ""
    tools: list[ToolSpec] = field(default_factory=list)
    max_tokens: int = 16_000
    effort: str | None = None
    task_budget_tokens: int | None = None
    # Populated by the adapter when it had to fall back; surfaced in the report.
    degradations: list[str] = field(default_factory=list)


class ModelProvider(ABC):
    """One vendor, behind one interface."""

    name: str = "unnamed"
    capabilities: Capabilities = Capabilities()

    @classmethod
    def available(cls) -> tuple[bool, str]:
        """Whether this adapter can run: SDK installed and credentials present.

        Returns (ok, reason). An unavailable adapter reports itself at startup rather
        than crashing on import, so installing Gerent never requires every vendor's
        package.
        """
        return True, ""

    @abstractmethod
    def stream(self, req: CompletionRequest) -> AsyncIterator[ProviderEvent]:
        """Yield normalized events. Implementations are async generators."""
        raise NotImplementedError

    async def count_tokens(self, req: CompletionRequest) -> int:
        """Best-effort token estimate.

        The default is a deliberately crude ~4-chars-per-token heuristic. Adapters
        whose vendor exposes a real counting endpoint should override it; callers use
        this for budget accounting, where being roughly right beats being unavailable.
        """
        chars = len(req.system) + sum(
            len(b.text) if hasattr(b, "text") else 200 for m in req.messages for b in m.blocks
        )
        return chars // 4

    def note_degradation(self, req: CompletionRequest, what: str) -> None:
        if what not in req.degradations:
            req.degradations.append(what)
