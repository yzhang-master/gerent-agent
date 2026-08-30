"""Executing a plan durably.

Two properties the executor must hold, both from docs/planning.md:

- Persist after EVERY step, not at the end and not on a timer. The point of durability
  is a crash between any two steps.
- Run a step as an ordinary turn. A step is not a special execution mode; it is a
  TurnRequest with plan context attached. One loop, again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog

from gerent.core.config import Config
from gerent.core.errors import BudgetExhausted, KillSwitch
from gerent.core.kernel import Kernel
from gerent.core.types import EventKind, TurnEvent, TurnRequest
from gerent.planning.models import Plan, Step, StepStatus
from gerent.planning.planner import Planner
from gerent.planning.store import PlanStore
from gerent.reporting.journal import Journal, JournalKind

log = structlog.get_logger(__name__)


class Executor:
    def __init__(
        self,
        kernel: Kernel,
        planner: Planner,
        store: PlanStore,
        config: Config,
    ) -> None:
        self.kernel = kernel
        self.planner = planner
        self.store = store
        self.config = config

    async def run(
        self, plan: Plan, request: TurnRequest, journal: Journal
    ) -> AsyncIterator[TurnEvent]:
        await self.store.save_plan(plan)
        replans = plan.replans

        while not plan.finished:
            ready = plan.ready()
            if not ready:
                # Nothing runnable: either dependencies failed, or the DAG is stuck.
                for step in plan.blocked_by_failure():
                    step.status = StepStatus.SKIPPED
                    step.result = "skipped: a dependency failed"
                    await self.store.save_step(step)
                    journal.append(
                        JournalKind.STEP_END, step_id=step.id, status="skipped",
                        description=step.description,
                    )
                if not plan.ready():
                    break
                continue

            # Filesystem work serializes: two parallel steps checkpointing the same
            # workspace produce an ambiguous restore point. Only the first ready step
            # runs at a time until read-only classification exists.
            for step in ready[:1]:
                async for event in self._run_step(plan, step, request, journal):
                    yield event

                if step.status is StepStatus.FAILED:
                    if replans < self.config.planning.max_replans:
                        replans += 1
                        yield TurnEvent(
                            kind=EventKind.THINKING,
                            text=f"replanning after: {step.result or 'step failed'}",
                        )
                        remaining = await self.planner.plan(
                            plan.goal,
                            journal=journal,
                            context=self._context(plan),
                            replan_of=plan,
                        )
                        remaining.replans = replans
                        remaining.session_id = plan.session_id
                        # Completed work is not re-planned or re-run; the new DAG
                        # covers the remainder only.
                        plan = remaining
                        await self.store.save_plan(plan)
                    else:
                        # Bounded: an agent that replans forever spends the whole
                        # budget on a task that was impossible from the start.
                        journal.append(
                            JournalKind.ERROR,
                            cause="replan_limit",
                            detail=f"gave up after {replans} replans",
                        )

        plan.status = plan.outcome()
        await self.store.save_plan(plan)

    async def _run_step(
        self, plan: Plan, step: Step, request: TurnRequest, journal: Journal
    ) -> AsyncIterator[TurnEvent]:
        step.status = StepStatus.RUNNING
        step.attempts += 1
        step.started_at = datetime.now(UTC)
        await self.store.save_step(step)
        journal.append(JournalKind.STEP_START, step_id=step.id, description=step.description)

        step_request = TurnRequest(
            session_id=request.session_id,
            source=request.source,
            text=self._step_prompt(plan, step),
            locale=request.locale,
            actor=request.actor,
            budget=request.budget,
            plan_id=plan.id,
            step_id=step.id,
        )

        failed = False
        try:
            async for event in self.kernel.run(step_request, journal=journal):
                if event.kind is EventKind.DONE:
                    continue  # one DONE per plan, emitted by the caller
                if event.kind is EventKind.ERROR:
                    failed = True
                yield event
        except (BudgetExhausted, KillSwitch):
            step.status = StepStatus.FAILED
            step.result = "run stopped before this step completed"
            step.ended_at = datetime.now(UTC)
            await self.store.save_step(step)
            raise

        step.status = StepStatus.FAILED if failed else StepStatus.DONE
        step.result = "failed" if failed else "completed"
        step.ended_at = datetime.now(UTC)
        # Written immediately: the point of durability is a crash between any two steps.
        await self.store.save_step(step)
        journal.append(
            JournalKind.STEP_END,
            step_id=step.id,
            status=str(step.status),
            description=step.description,
        )

        retriable = step.attempts < self.config.planning.max_step_attempts
        if step.status is StepStatus.FAILED and retriable:
            # Transient failures get one more try before the plan is rewritten.
            step.status = StepStatus.PENDING
            await self.store.save_step(step)

    def _step_prompt(self, plan: Plan, step: Step) -> str:
        done = [s for s in plan.steps if s.status is StepStatus.DONE]
        context = ""
        if done:
            # Summarised, not verbatim: a long plan would otherwise exceed any context
            # window by carrying every prior step's full output.
            context = "\n".join(f"- {s.description}: {s.result or 'done'}" for s in done[-5:])
            context = f"\n\nAlready completed:\n{context}"
        return (
            f"Overall goal: {plan.goal}{context}\n\n"
            f"Your task right now is only this step:\n{step.description}\n\n"
            "Do it now. Do not ask questions - if something is underdetermined, choose "
            "the most reasonable option and say what you assumed."
        )

    def _context(self, plan: Plan) -> str:
        return "\n".join(
            f"- [{s.status}] {s.description}: {s.result or ''}" for s in plan.steps
        )

    async def resume(self) -> list[Plan]:
        """Reconcile plans left RUNNING by a crash.

        A step caught RUNNING died mid-execution and cannot know whether its side
        effect landed. Idempotent steps are re-run; the rest are failed so the planner
        can decide what to do, because assuming "it probably finished" is how you get
        double-sent emails.
        """
        resumed = []
        for plan in await self.store.running_plans():
            for step in plan.steps:
                if step.status is not StepStatus.RUNNING:
                    continue
                if step.idempotent:
                    step.status = StepStatus.PENDING
                    step.result = "re-run after crash (idempotent)"
                else:
                    step.status = StepStatus.FAILED
                    step.result = (
                        "interrupted by a crash; not re-run automatically because the "
                        "step is not idempotent and its effect is unknown"
                    )
                await self.store.save_step(step)
            resumed.append(plan)
        return resumed
