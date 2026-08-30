"""Recording assumptions.

Without this the agent has no way to say "I decided X because Y", and the report's
assumptions section is permanently empty - which would quietly turn "decide and record"
back into "decide silently". See docs/autonomy.md#deciding-instead-of-asking.
"""

from __future__ import annotations

from gerent.core.types import Assumption, EventKind, TurnEvent
from gerent.reporting.journal import JournalKind
from gerent.skills.base import Risk, Skill, SkillContext, SkillResult


class NoteAssumption(Skill):
    name = "note_assumption"
    description = (
        "Record a decision you made without asking, when something was underdetermined. "
        "Use it whenever you picked between reasonable options - which library, which "
        "file, which interpretation of an ambiguous request. It appears in the final "
        "report so the human can check it. Recording an assumption is never a reason to "
        "stop working; note it and carry on."
    )
    risk = Risk.WRITE
    defer_loading = False
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What was ambiguous."},
            "chosen": {"type": "string", "description": "What you decided."},
            "rationale": {"type": "string", "description": "Why - cite workspace evidence."},
            "confidence": {
                "type": "number",
                "description": "0.0 to 1.0. Be honest; low-confidence ones sort first.",
            },
        },
        "required": ["question", "chosen", "rationale"],
        "additionalProperties": False,
    }

    async def run(
        self,
        ctx: SkillContext,
        question: str = "",
        chosen: str = "",
        rationale: str = "",
        confidence: float = 0.5,
    ) -> SkillResult:
        assumption = Assumption(
            question=question,
            chosen=chosen,
            rationale=rationale,
            confidence=max(0.0, min(1.0, float(confidence))),
        )
        ctx.journal.append(
            JournalKind.ASSUMPTION,
            question=assumption.question,
            chosen=assumption.chosen,
            rationale=assumption.rationale,
            confidence=assumption.confidence,
        )
        if ctx.emit:
            await ctx.emit(
                TurnEvent(
                    kind=EventKind.ASSUMPTION,
                    text=f"{question} → {chosen}",
                    data={"confidence": assumption.confidence},
                )
            )
        return SkillResult(ok=True, content=f"noted: {question} → {chosen}")
