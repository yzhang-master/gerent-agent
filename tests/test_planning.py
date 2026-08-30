"""Plan durability, resumption, and the no-blocked-state rule."""

import uuid

import pytest

from gerent.core.config import Config
from gerent.core.types import Actor, Source, TurnRequest, Usage
from gerent.planning.executor import Executor
from gerent.planning.models import Plan, PlanStatus, Step, StepStatus
from gerent.planning.planner import Planner, needs_plan
from gerent.planning.store import InMemoryPlanStore
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.base import Stop, StopReason, ToolCallComplete, UsageReport
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router
from gerent.reporting.journal import Journal, JournalKind
from gerent.reporting.reporter import build_report


def plan_provider(steps):
    """A provider whose first turn submits a plan, then answers each step."""
    provider = FakeProvider()
    provider.script(
        ToolCallComplete(id="p", name="submit_plan", arguments={"steps": steps}),
        UsageReport(Usage()),
        Stop(StopReason.TOOL_USE),
    )
    for _ in steps:
        provider.say("step done")
    return provider


def make_executor(config, workspace, provider):
    from gerent.core.kernel import Kernel
    from gerent.skills.registry import SkillRegistry

    router = Router(config)
    router.register("fake", provider)
    engine = Engine(router)
    kernel = Kernel(config, engine, SkillRegistry(config.skills).discover(), workspace=workspace)
    store = InMemoryPlanStore()
    return Executor(kernel, Planner(engine), store, config), store


def a_request():
    return TurnRequest(
        session_id=uuid.uuid4(), source=Source.CLI, text="goal", actor=Actor(name="t")
    )


# ── the DAG ──────────────────────────────────────────────────────────────────


def test_no_blocked_state_exists():
    """A gated agent blocks on ambiguity; a human worker decides. ADR 0002."""
    assert not hasattr(StepStatus, "BLOCKED")
    assert set(StepStatus) == {
        StepStatus.PENDING,
        StepStatus.RUNNING,
        StepStatus.DONE,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
    }


def test_dependents_of_a_failed_step_are_skipped_not_stuck():
    a = Step("a")
    b = Step("b", depends_on=[a.id])
    plan = Plan(goal="g", steps=[a, b])
    a.status = StepStatus.FAILED
    assert plan.ready() == []
    assert plan.blocked_by_failure() == [b]


def test_forward_dependencies_are_dropped_rather_than_deadlocking():
    """A model can emit a dependency on a later step; a cycle would hang the executor."""
    import asyncio

    config = Config.model_validate(
        {"providers": {"fake": {"enabled": True}}, "roles": {"planner": ["fake:m"]}}
    )
    router = Router(config)
    router.register("fake", plan_provider([
        {"description": "first", "depends_on": [1]},   # forward reference
        {"description": "second", "depends_on": [0]},
    ]))
    plan = asyncio.run(Planner(Engine(router)).plan("g", journal=Journal()))
    assert plan.steps[0].depends_on == [], "a forward dependency must be dropped"
    assert plan.steps[1].depends_on == [plan.steps[0].id]
    assert plan.ready(), "the plan must have at least one runnable step"


# ── planning worthiness ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("what is the capital of Poland?", False),
        ("read hello.txt", False),
        ("add a test for the parser then run the suite", True),
    ],
)
def test_plan_worthiness_is_biased_against_planning(goal, expected):
    assert needs_plan(goal) is expected


# ── execution ────────────────────────────────────────────────────────────────


async def test_executor_persists_after_every_step(config, workspace):
    saved: list[tuple[str, str]] = []
    executor, store = make_executor(
        config, workspace, plan_provider([{"description": "one"}, {"description": "two"}])
    )
    original = store.save_step

    async def spy(step):
        saved.append((step.description, str(step.status)))
        await original(step)

    store.save_step = spy
    plan = await Planner(executor.planner.engine).plan("g", journal=Journal())
    async for _ in executor.run(plan, a_request(), Journal()):
        pass

    # RUNNING then DONE for each step - written as it goes, not batched at the end.
    assert ("one", "running") in saved and ("one", "done") in saved
    assert saved.index(("one", "done")) < saved.index(("two", "running"))


async def test_resume_reruns_idempotent_steps_and_fails_the_rest(config, workspace):
    """A step caught RUNNING cannot know whether its side effect landed."""
    executor, store = make_executor(config, workspace, FakeProvider())
    safe = Step("read a file", idempotent=True, status=StepStatus.RUNNING)
    risky = Step("send an email", idempotent=False, status=StepStatus.RUNNING)
    plan = Plan(goal="g", status=PlanStatus.RUNNING, steps=[safe, risky])
    await store.save_plan(plan)

    await executor.resume()

    assert safe.status is StepStatus.PENDING, "idempotent work is safe to re-run"
    assert risky.status is StepStatus.FAILED, "non-idempotent work must not be re-run blind"
    assert "not idempotent" in (risky.result or "")


async def test_assumptions_reach_the_report(config, workspace):
    """Without this the report's assumptions section is permanently empty, which turns
    'decide and record' back into 'decide silently'."""
    from gerent.core.kernel import Kernel
    from gerent.skills.registry import SkillRegistry

    provider = FakeProvider().call_tool(
        "note_assumption",
        question="Which test runner?",
        chosen="pytest",
        rationale="pyproject already depends on it",
        confidence=0.9,
    ).say("done")
    router = Router(config)
    router.register("fake", provider)
    kernel = Kernel(
        config, Engine(router), SkillRegistry(config.skills).discover(), workspace=workspace
    )
    journal = Journal(goal="add a test")
    async for _ in kernel.run(a_request(), journal=journal):
        pass

    assert journal.of_kind(JournalKind.ASSUMPTION)
    report = build_report(journal)
    assert report.assumptions and report.assumptions[0]["chosen"] == "pytest"
    assert "Which test runner?" in report.to_markdown()
