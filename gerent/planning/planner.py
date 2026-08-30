"""Turning a goal into a DAG - and deciding whether that is even worth doing.

Most turns should not produce a plan. Over-planning doubles latency on trivial requests
and produces four-step plans for one-step jobs, so the heuristic is deliberately biased
toward not planning. See docs/planning.md.
"""

from __future__ import annotations

import structlog

from gerent.core.types import Msg, Role, TextBlock
from gerent.planning.models import Plan, Step
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.base import ToolSpec
from gerent.reporting.journal import Journal, JournalKind

log = structlog.get_logger(__name__)

SUBMIT_PLAN = ToolSpec(
    name="submit_plan",
    description=(
        "Submit the plan for the goal as an ordered list of steps. Each step is one "
        "unit of work a capable worker could carry out and verify."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What to do, concretely and verifiably.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Indices of earlier steps this one needs.",
                        },
                        "idempotent": {
                            "type": "boolean",
                            "description": (
                                "True only if re-running is harmless. Read-only work is "
                                "idempotent; sending, posting or appending is not."
                            ),
                        },
                    },
                    "required": ["description"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["steps"],
        "additionalProperties": False,
    },
)

PLANNER_SYSTEM = """You decompose a goal into the smallest plan that actually accomplishes it.

Rules:
- Prefer few steps. Three good steps beat eight timid ones.
- Each step must be concrete and verifiable, not a category of work.
- Mark a step idempotent only when re-running it is genuinely harmless.
- Use depends_on only for real ordering constraints; independent steps should stay \
independent so they can run in parallel.
- Do not add steps that ask the user anything. There is no user to ask: decide, and \
note the assumption in the step description.

Call submit_plan exactly once."""

# Signals that a goal is a single action rather than a project. Cheap and lexical on
# purpose - a model call to decide whether to make a model call is a poor trade.
_SIMPLE_HINTS = (
    "what is", "what's", "who is", "when is", "where is", "how do i", "explain",
    "show me", "list ", "read ", "print ", "tell me", "define ", "summarise",
    "summarize", "?",
)


def needs_plan(goal: str) -> bool:
    """Whether this goal is worth planning. Biased toward no."""
    text = goal.strip().lower()
    if len(text) < 40 and any(h in text for h in _SIMPLE_HINTS):
        return False
    multi = sum(text.count(sep) for sep in (" then ", " and then ", ", then ", ";"))
    long_enough = len(text.split()) > 25
    imperative_chain = multi >= 1
    return bool(long_enough or imperative_chain)


class Planner:
    def __init__(self, engine: Engine, role: str = "planner") -> None:
        self.engine = engine
        self.role = role

    async def plan(
        self, goal: str, *, journal: Journal, context: str = "", replan_of: Plan | None = None
    ) -> Plan:
        prompt = f"Goal:\n{goal}"
        if context:
            prompt += f"\n\nWhat has happened so far:\n{context}"
        if replan_of:
            prompt += (
                "\n\nA previous plan failed. Write a new plan for the REMAINING work "
                "only - do not repeat steps already completed."
            )

        completion = await self.engine.call(
            self.role,
            [Msg(role=Role.USER, blocks=[TextBlock(text=prompt)])],
            system=PLANNER_SYSTEM,
            tools=[SUBMIT_PLAN],
            journal=journal,
        )

        raw_steps: list[dict] = []
        for call in completion.message.tool_calls():
            if call.name == "submit_plan":
                raw_steps = list(call.arguments.get("steps") or [])
                break

        if not raw_steps:
            # The planner declined to produce a plan. Rather than fail the run, treat
            # the goal as a single step - the executor handles it as an ordinary turn.
            log.warning("planner.no_plan", goal=goal[:80])
            raw_steps = [{"description": goal, "idempotent": False}]

        plan = Plan(goal=goal, replans=(replan_of.replans + 1) if replan_of else 0)
        steps: list[Step] = []
        for index, raw in enumerate(raw_steps):
            steps.append(
                Step(
                    description=str(raw.get("description", "")).strip() or f"step {index + 1}",
                    seq=index,
                    plan_id=plan.id,
                    idempotent=bool(raw.get("idempotent", False)),
                )
            )
        # Indices are resolved to ids only after every step exists, and any dependency
        # pointing forward or at itself is dropped rather than creating a cycle the
        # executor would deadlock on.
        for index, raw in enumerate(raw_steps):
            for dep in raw.get("depends_on") or []:
                if isinstance(dep, int) and 0 <= dep < index:
                    steps[index].depends_on.append(steps[dep].id)

        plan.steps = steps
        journal.append(
            JournalKind.REPLAN if replan_of else JournalKind.PLAN,
            goal=goal,
            steps=[s.description for s in steps],
        )
        return plan
