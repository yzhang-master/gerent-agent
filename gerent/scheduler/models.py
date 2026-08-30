"""Schedules and the next-firing computation.

Timezones are IANA names, never offsets: "Europe/Warsaw" is +01:00 for part of the year
and +02:00 for the rest, so storing an offset makes 09:00 silently become 08:00 or 10:00
after a transition.

The two DST transitions are handled explicitly, because this is the bug class that
surfaces twice a year on a machine nobody is watching. See docs/scheduling.md#timezones.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from croniter import croniter

from gerent.core.types import new_id


class ScheduleKind(StrEnum):
    CRON = "cron"
    ONCE = "once"
    INTERVAL = "interval"


@dataclass
class Schedule:
    prompt: str
    kind: ScheduleKind = ScheduleKind.CRON
    expr: str = "0 9 * * *"
    timezone: str = "UTC"
    id: uuid.UUID = field(default_factory=new_id)
    actor_id: uuid.UUID | None = None
    report_sink: str | None = None
    retry_depth: int = 0
    next_fire_at: datetime | None = None
    last_fired_at: datetime | None = None
    running: bool = False
    enabled: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_interval(expr: str) -> timedelta:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    suffix = expr[-1].lower()
    if suffix not in units:
        raise ValueError(f"interval {expr!r} must end in s, m, h or d")
    return timedelta(**{units[suffix]: float(expr[:-1])})


def _is_nonexistent(naive: datetime, tz: ZoneInfo) -> bool:
    """True inside a spring-forward gap - the wall-clock time never occurs."""
    localized = naive.replace(tzinfo=tz)
    return localized.astimezone(UTC).astimezone(tz).replace(tzinfo=tz) != localized


def _is_ambiguous(naive: datetime, tz: ZoneInfo) -> bool:
    """True inside a fall-back repeat - the wall-clock time occurs twice."""
    return naive.replace(tzinfo=tz, fold=0).utcoffset() != naive.replace(
        tzinfo=tz, fold=1
    ).utcoffset()


def localize(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach `tz` to a wall-clock time, resolving both DST edge cases.

    - Spring forward: the time does not exist, so fire once at the first valid instant
      after the gap rather than skipping the day entirely.
    - Fall back: the time happens twice, so fire once, on the first occurrence.
    """
    if _is_nonexistent(naive, tz):
        # Walk forward a minute at a time to the far side of the gap. Gaps are an hour
        # at most in every real zone, so this terminates quickly.
        probe = naive
        for _ in range(180):
            probe += timedelta(minutes=1)
            if not _is_nonexistent(probe, tz):
                return probe.replace(tzinfo=tz)
    # fold=0 is the first (pre-transition) occurrence of an ambiguous time.
    return naive.replace(tzinfo=tz, fold=0)


def next_fire(schedule: Schedule, after: datetime | None = None) -> datetime | None:
    """The next firing, in UTC, computed in the schedule's own zone."""
    now = (after or datetime.now(UTC)).astimezone(UTC)
    tz = schedule.tz

    if schedule.kind is ScheduleKind.ONCE:
        when = datetime.fromisoformat(schedule.expr)
        if when.tzinfo is None:
            when = localize(when, tz)
        # A one-shot already in the past has no next firing.
        return when.astimezone(UTC) if when.astimezone(UTC) > now else None

    if schedule.kind is ScheduleKind.INTERVAL:
        base = schedule.last_fired_at or now
        return (base.astimezone(UTC) + parse_interval(schedule.expr)).astimezone(UTC)

    local_now = now.astimezone(tz).replace(tzinfo=None)
    cursor = croniter(schedule.expr, local_now)
    naive_next = cursor.get_next(datetime)
    return localize(naive_next, tz).astimezone(UTC)
