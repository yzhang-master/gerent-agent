# 0007 — Custom scheduler over a scheduling library

**Status:** Accepted

## Context

Recurring work needs cron, one-shot, and interval triggers. Mature libraries exist
(APScheduler and similar), several with database-backed job stores.

## Decision

A `schedules` table plus one poller built on `croniter` and `zoneinfo`.

## Consequences

**Gained:** three properties that are requirements here rather than preferences.

1. Schedules are **rows the agent can read and write** through the `schedule` skill.
   Self-scheduling — "retry this tomorrow" — is the feature that makes the agent feel
   like a colleague, and it cannot work against jobs held in a runtime object.
2. **Per-schedule IANA timezones**, not a process-wide setting. Correct DST handling
   across spring-forward and fall-back is explicit and tested.
3. `FOR UPDATE SKIP LOCKED` means multiple poller processes never double-fire — the
   property that makes horizontal scale possible later.

**Paid:** we own the firing semantics, including the decisions libraries make for you —
missed firings fire **once** on recovery rather than once per missed interval, and a
schedule whose previous run is still going skips rather than stacking. Both need tests.
Poll granularity is bounded by the poll interval, which is fine for cron-shaped work
and would not be for sub-second triggers.

## Rejected

*APScheduler with a SQLAlchemy job store.* Durable, but jobs are library-managed
objects rather than domain rows the agent can reason about, and it would pull in an ORM
the project otherwise does not use.

*System cron.* No introspection, no self-scheduling, no per-schedule timezone, and it
would put the trigger outside the process that has to report on it.
