"""Plan and Step.

A plan is a DAG, and it is durable. The state machine has no `blocked` state: a gated
agent blocks on ambiguity, a human worker decides and records an assumption. See
docs/planning.md and ADR 0004.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from gerent.core.types import new_id


class PlanStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ABANDONED = "abandoned"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Step:
    description: str
    id: uuid.UUID = field(default_factory=new_id)
    plan_id: uuid.UUID | None = None
    seq: int = 0
    depends_on: list[uuid.UUID] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    # Whether re-running after an ambiguous crash is safe. A step caught RUNNING at
    # startup cannot know whether its side effect landed; guessing "it probably
    # finished" is how you get double-sent emails.
    idempotent: bool = False
    attempts: int = 0
    result: str | None = None
    checkpoint_ref: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)


@dataclass
class Plan:
    goal: str
    id: uuid.UUID = field(default_factory=new_id)
    session_id: uuid.UUID | None = None
    status: PlanStatus = PlanStatus.RUNNING
    replans: int = 0
    steps: list[Step] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def by_id(self, step_id: uuid.UUID) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def ready(self) -> list[Step]:
        """Steps whose dependencies are all done."""
        out = []
        for step in self.steps:
            if step.status is not StepStatus.PENDING:
                continue
            deps = [self.by_id(d) for d in step.depends_on]
            if all(d is not None and d.status is StepStatus.DONE for d in deps):
                out.append(step)
        return out

    def blocked_by_failure(self) -> list[Step]:
        """Pending steps that can never run because a dependency failed or was skipped."""
        out = []
        for step in self.steps:
            if step.status is not StepStatus.PENDING:
                continue
            for dep_id in step.depends_on:
                dep = self.by_id(dep_id)
                if dep is not None and dep.status in (StepStatus.FAILED, StepStatus.SKIPPED):
                    out.append(step)
                    break
        return out

    @property
    def finished(self) -> bool:
        return all(s.terminal for s in self.steps)

    def outcome(self) -> PlanStatus:
        if any(s.status is StepStatus.FAILED for s in self.steps):
            return PlanStatus.FAILED
        return PlanStatus.DONE
