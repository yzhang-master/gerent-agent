"""The two vocabularies that cross every boundary, plus the normalized message format.

Nothing in this module may import a vendor SDK. These types are what the kernel, the
skills, and the database speak; adapters translate to and from vendor formats at the
provider boundary and nowhere else.

See docs/architecture.md and docs/providers.md.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

_last_ms = 0
_counter = 0


def new_id() -> uuid.UUID:
    """UUIDv7: 48-bit millisecond timestamp, then randomness.

    Time-ordered, so B-tree index locality comes free on every table keyed by one.
    Uses the spec's optional monotonic counter so ids generated inside the same
    millisecond still sort in creation order - without it, a burst of rows written in
    one millisecond lands in random index order.
    """
    global _last_ms, _counter
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    if ms == _last_ms:
        _counter += 1
    else:
        _last_ms, _counter = ms, secrets.randbits(10)
    rand_a = _counter & 0xFFF
    rand_b = secrets.randbits(62)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


# ── Normalized message content ───────────────────────────────────────────────
# Vendor block formats are translated into these. Persisted as JSONB in
# messages.blocks; see docs/data-model.md.


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    text: str


class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    media_type: str
    data: str  # base64


Block = Annotated[
    TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | ImageBlock,
    Field(discriminator="type"),
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Msg(BaseModel):
    """One message in normalized form.

    `provider_meta` carries vendor data that must survive a round trip to the *same*
    provider (thinking-block signatures being the motivating case). It is keyed by
    provider name and dropped when re-rendering to a different provider - another
    vendor cannot validate another vendor's signatures. See ADR 0001.
    """

    role: Role
    blocks: list[Block] = Field(default_factory=list)
    provider_meta: dict[str, Any] = Field(default_factory=dict)

    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]

    def for_provider(self, provider: str) -> Msg:
        """Strip provider_meta that does not belong to `provider`."""
        meta = {provider: self.provider_meta[provider]} if provider in self.provider_meta else {}
        return Msg(role=self.role, blocks=self.blocks, provider_meta=meta)


# ── What comes in ────────────────────────────────────────────────────────────


class Source(StrEnum):
    CLI = "cli"
    VOICE = "voice"
    API = "api"
    SCHEDULER = "scheduler"
    WEBHOOK = "webhook"


class Actor(BaseModel):
    """Who work is done on behalf of. The tenancy seam - present from day one though
    v1 is single-user, because threading identity through later means touching every
    function that handles a turn."""

    id: uuid.UUID = Field(default_factory=new_id)
    name: str = "local"


class Budget(BaseModel):
    """A ceiling on a run. Exhaustion ends the run cleanly *with* a report; never
    mid-write. See docs/autonomy.md."""

    max_wall_clock_s: float | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_tool_calls: int | None = None


class TurnRequest(BaseModel):
    session_id: uuid.UUID
    source: Source
    text: str | None = None
    locale: str | None = None  # BCP-47; None means "detect it"
    actor: Actor = Field(default_factory=Actor)
    deadline: datetime | None = None
    budget: Budget | None = None
    plan_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None

    model_config = {"arbitrary_types_allowed": True}


AudioStream = AsyncIterator[bytes]


# ── What goes out ────────────────────────────────────────────────────────────
# One vocabulary every port knows how to render. Adding a port means implementing
# this table and nothing else.


class EventKind(StrEnum):
    THINKING = "thinking"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASSUMPTION = "assumption"
    DEGRADATION = "degradation"
    REPORT = "report"
    ERROR = "error"
    DONE = "done"


class TurnEvent(BaseModel):
    kind: EventKind
    session_id: uuid.UUID | None = None
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text_delta(cls, text: str, **kw: Any) -> TurnEvent:
        return cls(kind=EventKind.TEXT_DELTA, text=text, **kw)

    @classmethod
    def thinking(cls, text: str, **kw: Any) -> TurnEvent:
        return cls(kind=EventKind.THINKING, text=text, **kw)

    @classmethod
    def done(cls, **kw: Any) -> TurnEvent:
        return cls(kind=EventKind.DONE, **kw)

    @classmethod
    def error(cls, text: str, **kw: Any) -> TurnEvent:
        return cls(kind=EventKind.ERROR, text=text, **kw)


# ── Assumptions ──────────────────────────────────────────────────────────────


class Assumption(BaseModel):
    """A decision made unilaterally where a gated agent would have asked.

    There is deliberately no `blocked` plan state; this is what replaces it.
    See docs/autonomy.md#deciding-instead-of-asking.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    question: str
    chosen: str
    rationale: str
    confidence: float = 0.5
    step_id: uuid.UUID | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )
