"""Plan persistence.

PostgresPlanStore is the real one: status is written after EVERY step, so a crash
between any two steps costs the report rather than the work, and a plan left RUNNING is
resumed on startup.

InMemoryPlanStore exists for tests and for running before a database is configured. It
is not durable, and the caller is told so loudly rather than silently downgraded -
ADR 0004 rejects in-memory plans as the operating mode.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from gerent.planning.models import Plan, PlanStatus, Step, StepStatus


class PlanStore(ABC):
    durable: bool = True

    @abstractmethod
    async def save_plan(self, plan: Plan) -> None: ...

    @abstractmethod
    async def save_step(self, step: Step) -> None: ...

    @abstractmethod
    async def load_plan(self, plan_id: uuid.UUID) -> Plan | None: ...

    @abstractmethod
    async def running_plans(self) -> list[Plan]: ...


class InMemoryPlanStore(PlanStore):
    durable = False

    def __init__(self) -> None:
        self._plans: dict[uuid.UUID, Plan] = {}

    async def save_plan(self, plan: Plan) -> None:
        self._plans[plan.id] = plan

    async def save_step(self, step: Step) -> None:
        return None  # steps are held by reference on the plan

    async def load_plan(self, plan_id: uuid.UUID) -> Plan | None:
        return self._plans.get(plan_id)

    async def running_plans(self) -> list[Plan]:
        return [p for p in self._plans.values() if p.status is PlanStatus.RUNNING]


class PostgresPlanStore(PlanStore):
    durable = True

    def __init__(self, db: Any) -> None:
        self._db = db

    async def save_plan(self, plan: Plan) -> None:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO plans (id, session_id, goal, status, replans, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE
                    SET status = EXCLUDED.status,
                        replans = EXCLUDED.replans,
                        updated_at = now()
                """,
                plan.id, plan.session_id, plan.goal, str(plan.status), plan.replans,
                plan.created_at,
            )
            for step in plan.steps:
                await self._upsert_step(conn, plan.id, step)

    async def save_step(self, step: Step) -> None:
        async with self._db.pool.acquire() as conn:
            await self._upsert_step(conn, step.plan_id, step)

    async def _upsert_step(self, conn: Any, plan_id: uuid.UUID | None, step: Step) -> None:
        await conn.execute(
            """
            INSERT INTO steps (id, plan_id, seq, description, depends_on, status,
                               idempotent, attempts, result, checkpoint_ref,
                               started_at, ended_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (id) DO UPDATE
                SET status = EXCLUDED.status,
                    attempts = EXCLUDED.attempts,
                    result = EXCLUDED.result,
                    checkpoint_ref = EXCLUDED.checkpoint_ref,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at
            """,
            step.id, plan_id, step.seq, step.description, step.depends_on,
            str(step.status), step.idempotent, step.attempts, step.result,
            step.checkpoint_ref, step.started_at, step.ended_at,
        )

    async def load_plan(self, plan_id: uuid.UUID) -> Plan | None:
        async with self._db.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM plans WHERE id = $1", plan_id)
            if row is None:
                return None
            steps = await conn.fetch(
                "SELECT * FROM steps WHERE plan_id = $1 ORDER BY seq", plan_id
            )
        return _hydrate(row, steps)

    async def running_plans(self) -> list[Plan]:
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM plans WHERE status = $1", str(PlanStatus.RUNNING)
            )
            out = []
            for row in rows:
                steps = await conn.fetch(
                    "SELECT * FROM steps WHERE plan_id = $1 ORDER BY seq", row["id"]
                )
                out.append(_hydrate(row, steps))
        return out


def _hydrate(row: Any, step_rows: Any) -> Plan:
    plan = Plan(
        id=row["id"],
        session_id=row["session_id"],
        goal=row["goal"],
        status=PlanStatus(row["status"]),
        replans=row["replans"],
        created_at=row["created_at"] or datetime.now(UTC),
    )
    plan.steps = [
        Step(
            id=s["id"],
            plan_id=s["plan_id"],
            seq=s["seq"],
            description=s["description"],
            depends_on=list(s["depends_on"] or []),
            status=StepStatus(s["status"]),
            idempotent=s["idempotent"],
            attempts=s["attempts"],
            result=s["result"],
            checkpoint_ref=s["checkpoint_ref"],
            started_at=s["started_at"],
            ended_at=s["ended_at"],
        )
        for s in step_rows
    ]
    return plan
