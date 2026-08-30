"""Scheduler: DST correctness, single-fire on recovery, and overlap.

The DST cases are the ones that surface twice a year on a machine nobody is watching.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gerent.core.kernel import Kernel
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router
from gerent.scheduler.models import Schedule, ScheduleKind, next_fire
from gerent.scheduler.service import Scheduler
from gerent.scheduler.store import InMemoryScheduleStore
from gerent.skills.registry import SkillRegistry

WARSAW = ZoneInfo("Europe/Warsaw")


def daily_at_0230() -> Schedule:
    return Schedule(prompt="x", expr="30 2 * * *", timezone="Europe/Warsaw")


def test_timezone_must_be_iana_not_an_offset():
    from gerent.core.config import AgentConfig

    AgentConfig(timezone="Europe/Warsaw")
    with pytest.raises(ValueError):
        AgentConfig(timezone="+01:00")


def test_spring_forward_fires_once_after_the_gap_not_skipped():
    """On 2026-03-29 Warsaw jumps 02:00 -> 03:00, so 02:30 never occurs.

    The schedule must still fire that day, at the first valid instant.
    """
    before = datetime(2026, 3, 28, 12, 0, tzinfo=WARSAW)
    fired = next_fire(daily_at_0230(), before).astimezone(WARSAW)
    assert fired.date() == datetime(2026, 3, 29).date(), "the day must not be skipped"
    assert (fired.hour, fired.minute) == (3, 0), "must land just after the gap"


def test_fall_back_fires_once_not_twice():
    """On 2026-10-25 Warsaw repeats 02:00-03:00, so 02:30 happens twice."""
    cursor = datetime(2026, 10, 24, 12, 0, tzinfo=WARSAW)
    schedule = daily_at_0230()

    firings = []
    for _ in range(2):
        nxt = next_fire(schedule, cursor)
        firings.append(nxt)
        cursor = nxt

    on_transition_day = [f for f in firings if f.astimezone(WARSAW).date().day == 25]
    assert len(on_transition_day) == 1, "an ambiguous wall-clock time must fire once"
    # The first occurrence is the pre-transition one, still on summer time.
    assert on_transition_day[0].astimezone(WARSAW).utcoffset() == timedelta(hours=2)


def test_a_past_one_shot_has_no_next_firing():
    schedule = Schedule(
        prompt="x", kind=ScheduleKind.ONCE, expr="2020-01-01T00:00:00+00:00", timezone="UTC"
    )
    assert next_fire(schedule) is None


async def test_missed_firings_fire_once_on_recovery(config, workspace):
    """Waking to fourteen queued runs of a daily job is worse than missing thirteen."""
    router = Router(config)
    router.register("fake", FakeProvider().say("done"))
    kernel = Kernel(
        config, Engine(router), SkillRegistry(config.skills).discover(), workspace=workspace
    )
    store = InMemoryScheduleStore()
    scheduler = Scheduler(config, kernel, store)
    config.scheduler.report_sink = f"file:{workspace}/reports/"

    schedule = daily_at_0230()
    # Pretend the machine was off for two weeks.
    schedule.next_fire_at = datetime.now(UTC) - timedelta(days=14)
    await store.upsert(schedule)

    fired = await scheduler.tick()

    assert len(fired) == 1, "one catch-up firing, not one per missed day"
    reports = list((workspace / "reports").glob("*.md"))
    assert len(reports) == 1, "the report must be pushed, not left in a log"
    assert "late by" in reports[0].read_text()


async def test_running_schedule_is_not_fired_again(config, workspace):
    store = InMemoryScheduleStore()
    schedule = daily_at_0230()
    schedule.next_fire_at = datetime.now(UTC) - timedelta(minutes=1)
    schedule.running = True
    await store.upsert(schedule)
    assert await store.due(datetime.now(UTC)) == [], "overlapping runs must not stack"
