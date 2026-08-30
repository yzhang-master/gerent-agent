"""The kernel: reason, act, report.

Every port hands it the same TurnRequest and renders the same TurnEvent stream. The
loop is ours rather than a vendor helper's, because conversation history must be
inspectable, resumable, reportable, and re-renderable for failover - see ADR 0005.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import structlog

from gerent.core.config import Config
from gerent.core.errors import BudgetExhausted, GuardrailDenied, KillSwitch, ProviderError
from gerent.core.types import (
    Budget,
    EventKind,
    Msg,
    Role,
    TextBlock,
    ToolResultBlock,
    TurnEvent,
    TurnRequest,
)
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.base import StopReason
from gerent.reporting.journal import Journal, JournalKind
from gerent.skills.base import Risk, SkillContext, SkillResult
from gerent.skills.checkpoint import Checkpointer
from gerent.skills.guardrails import Guardrails, RunBudget
from gerent.skills.registry import SkillRegistry

log = structlog.get_logger(__name__)

MAX_TOOL_ITERATIONS = 50
MAX_PAUSE_RESTARTS = 5

SYSTEM_PROMPT = """You are Gerent, an autonomous agent that works like a capable colleague.

You are given a goal and you complete it end to end. You do not ask for permission and \
you do not stop to ask clarifying questions: when something is underdetermined, choose \
the most reasonable option based on evidence in the workspace, state the assumption \
plainly in your reply, and continue.

Work concretely. Prefer reading the workspace over guessing. When a tool fails, read \
the error and try a different approach rather than repeating the same call.

When you have finished, summarise what you did, what changed, what you assumed, and \
anything that failed or that you deliberately skipped. Report failures as plainly as \
successes."""


class Kernel:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        registry: SkillRegistry,
        *,
        workspace: Path | None = None,
        scheduler: object | None = None,
        memories: object | None = None,
    ) -> None:
        self.config = config
        self.engine = engine
        self.registry = registry
        self.guardrails = Guardrails(config.guardrails)
        self.workspace = workspace or config.guardrails.workspace_roots[0]
        self.scheduler = scheduler
        self.memories = memories
        self.history: dict[str, list[Msg]] = {}

    async def run(
        self, request: TurnRequest, *, role: str = "worker", journal: Journal | None = None
    ) -> AsyncIterator[TurnEvent]:
        """Run one turn to completion, streaming events.

        Every terminal condition still yields a report-able ending: budget exhaustion,
        the kill switch, a refusal, and provider failure all finish cleanly rather than
        leaving the caller with a half-stream and no explanation.
        """
        journal = journal or Journal(goal=request.text or "")
        journal.append(JournalKind.GOAL, text=request.text or "", source=str(request.source))

        budget_cfg = (
            self.config.budgets.scheduled
            if request.source.value == "scheduler"
            else self.config.budgets.interactive
        )
        run_budget = RunBudget(
            request.budget or Budget(**budget_cfg.model_dump())
        )
        checkpointer = Checkpointer(journal.run_id, self.config.guardrails.checkpoint_dir)

        key = str(request.session_id)
        messages = self.history.setdefault(key, [])

        # Relevant durable facts are injected automatically, which is what lets the
        # `recall` skill be reserved for things similarity search did not surface.
        # Volatile content goes AFTER the cached prefix, never into the system prompt.
        if context := await self._recall(request):
            messages.append(Msg(role=Role.SYSTEM, blocks=[TextBlock(text=context)]))

        messages.append(Msg(role=Role.USER, blocks=[TextBlock(text=request.text or "")]))

        events: list[TurnEvent] = []

        async def emit(event: TurnEvent) -> None:
            event.session_id = request.session_id
            events.append(event)

        pauses = 0
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                self.guardrails.check_kill_switch()
                run_budget.check()

                specs, degradation = self.registry.specs(
                    self._capabilities(role), query=request.text or ""
                )
                if degradation:
                    journal.append(JournalKind.DEGRADATION, detail=degradation)

                completion = await self.engine.call(
                    role,
                    messages,
                    system=SYSTEM_PROMPT,
                    tools=specs,
                    journal=journal,
                    emit=emit,
                )
                for event in events:
                    yield event
                events.clear()

                run_budget.add(completion.usage)
                messages.append(completion.message)

                if completion.stop is StopReason.REFUSAL:
                    yield TurnEvent.error(f"the model declined: {completion.detail}")
                    break

                if completion.stop is StopReason.PAUSE:
                    # A paused turn is not a finished turn. Unattended, treating it as
                    # one is indistinguishable from a truncated answer reported as
                    # success, so it is resumed explicitly and bounded.
                    pauses += 1
                    if pauses > MAX_PAUSE_RESTARTS:
                        yield TurnEvent.error("turn still paused after repeated resumes")
                        break
                    continue

                calls = completion.message.tool_calls()
                if not calls:
                    break

                results: list[ToolResultBlock] = []
                for call in calls:
                    self.guardrails.check_kill_switch()
                    run_budget.tool_calls += 1
                    result = await self._invoke(
                        call.name, call.arguments, request, journal, checkpointer
                    )
                    for event in events:
                        yield event
                    events.clear()
                    yield TurnEvent(
                        kind=EventKind.TOOL_RESULT,
                        session_id=request.session_id,
                        text=result.content[:400],
                        data={"skill": call.name, "ok": result.ok},
                    )
                    results.append(
                        ToolResultBlock(
                            tool_call_id=call.id,
                            content=result.content,
                            is_error=not result.ok,
                        )
                    )
                messages.append(Msg(role=Role.TOOL, blocks=results))
            else:
                yield TurnEvent.error(
                    f"stopped after {MAX_TOOL_ITERATIONS} tool iterations without finishing"
                )

        except BudgetExhausted as exc:
            # Cleanly: finish here and report, never mid-write.
            journal.append(JournalKind.ERROR, kind="budget", detail=str(exc))
            yield TurnEvent.error(f"budget exhausted: {exc}")
        except KillSwitch as exc:
            journal.append(JournalKind.ERROR, kind="kill_switch", detail=str(exc))
            yield TurnEvent.error(f"stopped: {exc}")
        except ProviderError as exc:
            journal.append(JournalKind.ERROR, kind="provider", detail=str(exc))
            yield TurnEvent.error(f"provider failure: {exc}")
        finally:
            journal.finish()

        yield TurnEvent.done(session_id=request.session_id)

    async def _recall(self, request: TurnRequest) -> str:
        if self.memories is None or not request.text:
            return ""
        try:
            found = await self.memories.recall(
                request.text, request.actor.id, self.config.memory.retrieval_top_k
            )
        except Exception:  # noqa: BLE001
            # Memory is an enhancement; losing it must not fail the turn.
            log.warning("memory.recall_failed", exc_info=True)
            return ""
        if not found:
            return ""
        facts = "\n".join(f"- {m.content}" for m in found)
        return f"Things you remember that may be relevant:\n{facts}"

    def _capabilities(self, role: str):
        from gerent.reasoning.providers.base import Capabilities

        try:
            return self.engine.router.routes(role)[0].provider.capabilities
        except Exception:
            return Capabilities()

    async def _invoke(
        self,
        name: str,
        arguments: dict,
        request: TurnRequest,
        journal: Journal,
        checkpointer: Checkpointer,
    ) -> SkillResult:
        skill = self.registry.get(name)
        if skill is None:
            return SkillResult.failure(f"no such skill: {name}")

        journal.append(
            JournalKind.TOOL_CALL,
            skill=name,
            arguments=json.dumps(arguments, default=str)[:1000],
            risk=str(skill.risk),
        )

        # Anything whose effect cannot be read off its arguments is checkpointed first.
        # This is what replaces the confirmation dialog.
        if not skill.reversible or skill.risk is Risk.DESTRUCTIVE:
            ref = await checkpointer.checkpoint(self.workspace, label=f"before {name}")
            journal.append(
                JournalKind.CHECKPOINT,
                skill=name,
                kind=ref.kind,
                ref=ref.ref,
                restore_command=ref.restore_command,
            )

        ctx = SkillContext(
            session_id=request.session_id,
            actor=request.actor,
            workspace=self.workspace,
            journal=journal,
            locale=request.locale or self.config.agent.default_locale,
            guardrails=self.guardrails,
            checkpointer=checkpointer,
            scheduler=self.scheduler,
            memories=self.memories,
        )

        try:
            result = await skill.run(ctx, **arguments)
        except GuardrailDenied as exc:
            journal.append(JournalKind.DENIAL, skill=name, rule=exc.rule, detail=str(exc))
            result = SkillResult.failure(str(exc))
        except TypeError as exc:
            # Wrong or missing arguments from the model: a bad turn, not a crash.
            result = SkillResult.failure(f"invalid arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("skill.failed", skill=name)
            result = SkillResult.failure(f"{name} raised {type(exc).__name__}: {exc}")

        journal.append(
            JournalKind.TOOL_RESULT,
            skill=name,
            ok=result.ok,
            content=result.content[:2000],
        )
        return result
