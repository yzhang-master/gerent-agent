# Scheduling

Time-triggered work: cron, one-shot, and interval. A scheduled firing produces the
**same `TurnRequest`** a microphone produces — which is why "remind me tomorrow" and
"run the weekly report" need no machinery beyond what already exists.

## Durable, not in-process

A `schedules` table plus one poller (`croniter` + `zoneinfo`), rather than an
in-process scheduler library.

Three requirements rule out the library approach:

1. **Survive restart.** A schedule set on Tuesday must fire on Friday across any number
   of restarts and crashes.
2. **Be introspectable by the agent itself.** The agent reads and writes its own
   schedules through the `schedule` skill; they cannot live in a runtime object it
   cannot see.
3. **Be timezone-correct per schedule**, not per process — the world-wide requirement.

```python
@dataclass
class Schedule:
    id: UUID
    actor: Actor
    kind: Kind                  # cron | once | interval
    expr: str                   # "0 9 * * 1-5" | ISO-8601 | "15m"
    timezone: str               # IANA, e.g. "Europe/Warsaw" — never a UTC offset
    prompt: str                 # becomes TurnRequest.text
    budget: Budget | None       # tighter than interactive by default
    report_sink: str            # where the report is pushed
    next_fire_at: datetime      # UTC, computed in `timezone`
    last_fired_at: datetime | None
    enabled: bool
```

## Timezones

`timezone` is an **IANA name**, never a fixed offset. `Europe/Warsaw` is +01:00 for
part of the year and +02:00 for the rest; storing `+01:00` means "09:00" silently
becomes 08:00 or 10:00 after a DST transition.

`next_fire_at` is stored in UTC and computed by interpreting the cron expression in the
schedule's own zone. Two transitions need explicit handling and explicit tests:

- **Spring forward** — 02:30 does not exist on the transition day. A schedule for 02:30
  fires once, at the first valid instant after the gap. It must not be skipped for the
  day.
- **Fall back** — 02:30 occurs twice. It fires **once**, on the first occurrence.

This is the class of bug that surfaces twice a year, in production, on a machine nobody
is watching. It gets tests.

## The poller

```
every N seconds:
  SELECT ... WHERE enabled AND next_fire_at <= now()
            FOR UPDATE SKIP LOCKED
  for each due schedule:
    build TurnRequest{source=SCHEDULER, actor, text=prompt, budget}
    hand to kernel
    recompute next_fire_at in the schedule's timezone
```

`FOR UPDATE SKIP LOCKED` means two poller instances never double-fire the same
schedule — the property that makes running more than one process safe later.

**Missed firings** (the machine was asleep, the process was down) fire **once** on
recovery, not once per missed interval. Waking to find fourteen queued runs of a daily
job is worse than missing thirteen of them. `last_fired_at` records the gap; the report
mentions it.

**Overlap.** If the previous run of a schedule is still going, the next firing is
skipped and noted, rather than stacking concurrent runs of the same job.

## Self-scheduling

The `schedule` skill lets the agent manage its own time — which is what makes it feel
like a colleague rather than a command:

- "Retry the deploy check in 20 minutes" → a `once` schedule
- "That failed because the API was down; try again tomorrow" → recovery without a human
- "Summarize this repo every weekday at 09:00" → a `cron` schedule

Self-scheduling has an obvious hazard: an agent that reschedules itself on every failure
can loop forever, burning budget unattended. Self-created schedules therefore carry a
retry depth, and a chain that keeps failing stops and reports instead of continuing.

## Unattended runs

Scheduled runs get the same autonomy as interactive ones — a worker needing supervision
at 03:00 is not a worker that runs at 03:00 — with two differences
([autonomy.md](autonomy.md#unattended-runs)):

- **Tighter default budgets.** Nobody is watching the spend.
- **The report is pushed**, not printed. A report that lands in a logfile nobody opens
  is indistinguishable from a run that never happened. Failures especially must arrive
  somewhere a human will actually see.
