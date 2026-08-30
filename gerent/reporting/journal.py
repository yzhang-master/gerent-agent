"""The journal: an append-only structured record, written as the run proceeds.

An agent that never interrupts must account for itself afterward, or it is
unauditable. This is the substrate the report is rendered from.

Written *during* the run, never reconstructed at the end - a journal assembled
afterward from memory is a story rather than a record, and does not exist at all if the
process dies. See docs/reporting.md.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from gerent.core.types import Usage, new_id

# Keys whose values are digested rather than stored. The journal is read by humans and
# may be pasted into an issue; a token that reaches it has effectively leaked.
_SECRET_KEY = re.compile(
    r"(api[-_]?key|secret|token|password|passwd|credential|authorization|bearer)", re.I
)
_MAX_VALUE_CHARS = 2_000


class JournalKind(StrEnum):
    GOAL = "goal"
    PLAN = "plan"
    REPLAN = "replan"
    STEP_START = "step_start"
    STEP_END = "step_end"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ASSUMPTION = "assumption"
    CHECKPOINT = "checkpoint"
    DEGRADATION = "degradation"
    MODEL_CALL = "model_call"
    DENIAL = "denial"
    ERROR = "error"


def digest(value: Any) -> str:
    raw = repr(value).encode("utf-8", "replace")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Digest secret-looking values and truncate very large ones."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if _SECRET_KEY.search(key):
            out[key] = digest(value)
        elif isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            out[key] = value[:_MAX_VALUE_CHARS] + f"… [{len(value)} chars total]"
        elif isinstance(value, dict):
            out[key] = redact(value)
        else:
            out[key] = value
    return out


@dataclass
class JournalEntry:
    kind: JournalKind
    payload: dict[str, Any]
    run_id: uuid.UUID
    step_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Journal:
    """One run's record.

    Held in memory and flushed to the `journal` table where a store is configured. The
    in-memory copy is what the reporter renders, so reporting works with no database -
    which is what lets M0/M1 run before M2 exists.
    """

    run_id: uuid.UUID = field(default_factory=new_id)
    goal: str = ""
    entries: list[JournalEntry] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    def append(
        self, kind: JournalKind, /, *, step_id: uuid.UUID | None = None, **payload: Any
    ) -> JournalEntry:
        # `kind` is positional-only so a payload key of the same name (a checkpoint's
        # kind, an error's kind) lands in the payload instead of colliding.
        entry = JournalEntry(
            kind=kind, payload=redact(payload), run_id=self.run_id, step_id=step_id
        )
        self.entries.append(entry)
        return entry

    def of_kind(self, *kinds: JournalKind) -> list[JournalEntry]:
        wanted = set(kinds)
        return [e for e in self.entries if e.kind in wanted]

    def add_usage(self, usage: Usage) -> None:
        self.usage = self.usage + usage

    @property
    def duration_s(self) -> float:
        end = self.ended_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    def finish(self) -> None:
        self.ended_at = datetime.now(UTC)
