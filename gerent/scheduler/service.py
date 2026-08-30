"""The poller.

A firing constructs the same TurnRequest a microphone produces - that is why
"remind me tomorrow" and "run the weekly report" need no machinery beyond the kernel.
See docs/scheduling.md.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

import structlog

from gerent.core.config import Config
from gerent.core.kernel import Kernel
from gerent.core.types import Actor, Budget, Source, TurnRequest
from gerent.reporting.journal import Journal
from gerent.reporting.reporter import build_report
from gerent.scheduler.models import Schedule, ScheduleKind, next_fire
from gerent.scheduler.store import ScheduleStore

log = structlog.get_logger(__name__)


class Scheduler:
    def __init__(self, config: Config, kernel: Kernel, store: ScheduleStore) -> None:
        self.config = config
        self.kernel = kernel
        self.store = store
        self._stopping = asyncio.Event()

    async def add(self, schedule: Schedule) -> Schedule:
        schedule.next_fire_at = next_fire(schedule)
        await self.store.upsert(schedule)
        log.info(
            "scheduler.added", expr=schedule.expr, tz=schedule.timezone,
            next=str(schedule.next_fire_at),
        )
        return schedule

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler.tick_failed")
            # A timeout here just means "nothing asked us to stop; poll again".
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.config.scheduler.poll_interval_s
                )

    def stop(self) -> None:
        self._stopping.set()

    async def tick(self, now: datetime | None = None) -> list[Schedule]:
        now = now or datetime.now(UTC)
        fired: list[Schedule] = []
        for schedule in await self.store.due(now):
            fired.append(schedule)
            await self._fire(schedule, now)
        return fired

    async def _fire(self, schedule: Schedule, now: datetime) -> None:
        missed = None
        if schedule.next_fire_at and (now - schedule.next_fire_at).total_seconds() > 120:
            # Fires ONCE on recovery, not once per missed interval. Waking to fourteen
            # queued runs of a daily job is worse than missing thirteen of them.
            missed = now - schedule.next_fire_at
            log.warning("scheduler.late", schedule=str(schedule.id), late_by=str(missed))

        schedule.running = True
        schedule.last_fired_at = now
        await self.store.upsert(schedule)

        budget = Budget(**self.config.budgets.scheduled.model_dump())
        request = TurnRequest(
            session_id=schedule.id,
            source=Source.SCHEDULER,
            text=schedule.prompt,
            actor=Actor(id=schedule.actor_id, name="scheduler") if schedule.actor_id
            else Actor(name="scheduler"),
            budget=budget,
        )
        journal = Journal(goal=schedule.prompt)

        try:
            async for _ in self.kernel.run(request, journal=journal):
                pass
        finally:
            journal.finish()
            schedule.running = False
            if schedule.kind is ScheduleKind.ONCE:
                schedule.enabled = False
                schedule.next_fire_at = None
            else:
                schedule.next_fire_at = next_fire(schedule, now)
            await self.store.upsert(schedule)

        await self._deliver(schedule, journal, missed)

    async def _deliver(self, schedule: Schedule, journal: Journal, missed) -> None:
        """Push the report.

        An unattended run whose report lands in a logfile nobody opens is
        indistinguishable from a run that never happened.
        """
        report = build_report(journal)
        text = report.to_markdown()
        if missed:
            text += f"\n\n_This run was late by {missed}; it fired once on recovery._\n"

        sink = schedule.report_sink or self.config.scheduler.report_sink
        if sink.startswith("file:"):
            directory = Path(sink.removeprefix("file:")).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (directory / f"{stamp}-{schedule.id}.md").write_text(text, encoding="utf-8")
        else:
            log.warning("scheduler.unsupported_sink", sink=sink)
            log.info("scheduler.report", report=text[:2000])
