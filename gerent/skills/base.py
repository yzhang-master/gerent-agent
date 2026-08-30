"""The Skill contract - the seam that makes the agent domain-wide.

The core knows nothing about any particular domain; it gains capability by having
packs dropped into domains/. See docs/skills.md.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from gerent.core.types import Actor, TurnEvent
from gerent.reasoning.providers.base import ToolSpec
from gerent.reporting.journal import Journal


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


class Cost(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Artifact:
    """Something produced by a skill. Referenced, never inlined into the model's view."""

    kind: str  # "file" | "diff" | "audio" | ...
    path: str
    summary: str = ""


@dataclass
class SkillResult:
    ok: bool
    content: str
    artifacts: list[Artifact] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def failure(cls, error: str) -> SkillResult:
        return cls(ok=False, content=error, error=error)


@dataclass
class SkillContext:
    """What a skill is given. Note what is absent: the kernel.

    A skill that reaches back into the loop that called it cannot be tested, reused, or
    run in a subprocess later.
    """

    session_id: uuid.UUID
    actor: Actor
    workspace: Path
    journal: Journal
    locale: str = "en-US"
    emit: Callable[[TurnEvent], Awaitable[None]] | None = None
    guardrails: Any = None  # Guardrails; untyped here to keep the import graph acyclic
    checkpointer: Any = None
    scheduler: Any = None   # present only when the scheduler runs in this process

    async def progress(self, text: str) -> None:
        if self.emit is not None:
            await self.emit(TurnEvent.thinking(text, session_id=self.session_id))


class Skill(ABC):
    name: str = ""
    description: str = ""
    domain: str = "core"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    risk: Risk = Risk.READ
    # False means a checkpoint is mandatory before this runs. Set it False whenever the
    # effect cannot be inferred from the arguments.
    reversible: bool = True
    defer_loading: bool = True
    cost_hint: Cost = Cost.LOW

    @abstractmethod
    async def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        """Do the work.

        Failure is a returned result, not an exception: raising kills the turn, while
        returning lets the agent read what went wrong and route around it - which is
        the entire point of an autonomous worker.
        """

    def to_spec(self) -> ToolSpec:
        """Render to the provider-neutral tool shape. Authored once, never twice."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            defer_loading=self.defer_loading,
        )

    def validate(self) -> None:
        """Checked at registration, not at first call."""
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"skill name {self.name!r} must be non-empty snake_case")
        if len(self.description) < 20:
            raise ValueError(
                f"skill {self.name!r}: description is a prompt, not a docstring - "
                "the model uses it to decide whether this is the right tool"
            )
        schema = self.input_schema
        if schema.get("type") != "object":
            raise ValueError(f"skill {self.name!r}: input_schema must be an object schema")
        if schema.get("additionalProperties") is not False:
            raise ValueError(
                f"skill {self.name!r}: input_schema needs additionalProperties: false"
            )
        # The portable subset every provider accepts. Exotic constructs fail by
        # producing subtly wrong arguments rather than by erroring, which is worse.
        for bad in ("oneOf", "anyOf", "allOf", "$ref", "not"):
            if bad in str(schema):
                raise ValueError(
                    f"skill {self.name!r}: {bad} is outside the portable JSON Schema "
                    "subset and breaks tool calling on some providers"
                )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Skill {self.name} domain={self.domain} risk={self.risk}>"
