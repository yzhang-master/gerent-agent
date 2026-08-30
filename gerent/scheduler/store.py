"""Schedule persistence."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from gerent.scheduler.models import Schedule, ScheduleKind


class ScheduleStore(ABC):
    @abstractmethod
    async def upsert(self, schedule: Schedule) -> None: ...

    @abstractmethod
    async def due(self, now: datetime) -> list[Schedule]: ...

    @abstractmethod
    async def all(self) -> list[Schedule]: ...

    @abstractmethod
    async def delete(self, schedule_id: uuid.UUID) -> bool: ...


class InMemoryScheduleStore(ScheduleStore):
    def __init__(self) -> None:
        self._items: dict[uuid.UUID, Schedule] = {}

    async def upsert(self, schedule: Schedule) -> None:
        self._items[schedule.id] = schedule

    async def due(self, now: datetime) -> list[Schedule]:
        return [
            s
            for s in self._items.values()
            if s.enabled and not s.running and s.next_fire_at and s.next_fire_at <= now
        ]

    async def all(self) -> list[Schedule]:
        return list(self._items.values())

    async def delete(self, schedule_id: uuid.UUID) -> bool:
        return self._items.pop(schedule_id, None) is not None


class PostgresScheduleStore(ScheduleStore):
    def __init__(self, db: Any) -> None:
        self._db = db

    async def upsert(self, schedule: Schedule) -> None:
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO schedules (id, actor_id, kind, expr, timezone, prompt,
                                       report_sink, retry_depth, next_fire_at,
                                       last_fired_at, running, enabled)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (id) DO UPDATE
                    SET expr = EXCLUDED.expr,
                        timezone = EXCLUDED.timezone,
                        prompt = EXCLUDED.prompt,
                        next_fire_at = EXCLUDED.next_fire_at,
                        last_fired_at = EXCLUDED.last_fired_at,
                        running = EXCLUDED.running,
                        enabled = EXCLUDED.enabled
                """,
                schedule.id, schedule.actor_id, str(schedule.kind), schedule.expr,
                schedule.timezone, schedule.prompt, schedule.report_sink,
                schedule.retry_depth, schedule.next_fire_at, schedule.last_fired_at,
                schedule.running, schedule.enabled,
            )

    async def due(self, now: datetime) -> list[Schedule]:
        """Claim due schedules.

        FOR UPDATE SKIP LOCKED means two pollers never double-fire the same schedule -
        the property that makes running more than one process safe later.
        """
        async with self._db.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                SELECT * FROM schedules
                WHERE enabled AND NOT running AND next_fire_at IS NOT NULL
                      AND next_fire_at <= $1
                FOR UPDATE SKIP LOCKED
                """,
                now,
            )
            if rows:
                await conn.execute(
                    "UPDATE schedules SET running = true WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in rows],
                )
        return [_hydrate(r) for r in rows]

    async def all(self) -> list[Schedule]:
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM schedules ORDER BY next_fire_at")
        return [_hydrate(r) for r in rows]

    async def delete(self, schedule_id: uuid.UUID) -> bool:
        async with self._db.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM schedules WHERE id = $1", schedule_id)
        return result.endswith("1")


def _hydrate(row: Any) -> Schedule:
    return Schedule(
        id=row["id"],
        actor_id=row["actor_id"],
        kind=ScheduleKind(row["kind"]),
        expr=row["expr"],
        timezone=row["timezone"],
        prompt=row["prompt"],
        report_sink=row["report_sink"],
        retry_depth=row["retry_depth"],
        next_fire_at=row["next_fire_at"],
        last_fired_at=row["last_fired_at"] or None,
        running=row["running"],
        enabled=row["enabled"],
    )
