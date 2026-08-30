"""Remembering and recalling durable facts.

Bias hard toward storing few, durable, general facts. An agent that stores everything it
sees fills semantic memory with transient noise that pollutes retrieval for months.
See docs/memory.md.
"""

from __future__ import annotations

from gerent.skills.base import Risk, Skill, SkillContext, SkillResult


class Remember(Skill):
    name = "remember"
    description = (
        "Store a durable fact worth carrying into future sessions: a stable preference, "
        "a project constraint, a correction the user made. Store few things and only "
        "lasting ones - not what you are doing right now, not file contents, not "
        "anything you could read again from the workspace."
    )
    risk = Risk.WRITE
    defer_loading = False
    input_schema = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "One self-contained fact, phrased to make sense months from now.",
            }
        },
        "required": ["fact"],
        "additionalProperties": False,
    }

    async def run(self, ctx: SkillContext, fact: str = "") -> SkillResult:
        store = getattr(ctx, "memories", None)
        if store is None:
            return SkillResult.failure("no memory store configured, so this cannot be saved")
        await store.remember(fact.strip(), ctx.actor.id)
        return SkillResult(ok=True, content=f"remembered: {fact.strip()}")


class Recall(Skill):
    name = "recall"
    description = (
        "Search durable facts you stored earlier. Relevant memories are injected "
        "automatically each turn, so use this only when you need something specific "
        "that automatic retrieval did not surface."
    )
    risk = Risk.READ
    defer_loading = False
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, ctx: SkillContext, query: str = "") -> SkillResult:
        store = getattr(ctx, "memories", None)
        if store is None:
            return SkillResult.failure("no memory store configured")
        found = await store.recall(query, ctx.actor.id)
        if not found:
            return SkillResult(ok=True, content="nothing relevant remembered")
        return SkillResult(
            ok=True, content="\n".join(f"- {m.content}" for m in found)
        )
