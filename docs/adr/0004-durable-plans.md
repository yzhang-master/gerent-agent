# 0004 — Plans are durable DAGs in Postgres

**Status:** Accepted

## Context

Multi-step goals need a plan. The cheap option is a list held in memory for the
duration of the run.

But this agent runs unattended for long stretches, can be killed at any moment, and can
be woken hours later by the scheduler to continue work.

## Decision

Persist plans and steps as rows. Write status after **every** step, not at the end.
Resume `running` plans on startup.

Steps found in `running` at startup are ambiguous — the process died mid-step and
cannot know whether the side effect landed. They are re-run if idempotent, otherwise
failed and replanned.

## Consequences

**Gained:** `kill -9` costs the report, not the work. A plan is inspectable while it
runs. A scheduled run can pick up what an earlier run left unfinished. Replan history
is auditable.

**Paid:** a database write between every step; more schema; and a genuinely awkward
question — whether a step that was mid-flight actually completed — that has no clean
answer, only a conservative default.

Concurrency is also constrained: parallel steps touching the filesystem would produce
ambiguous restore points, so filesystem steps serialize and only read-only or external
steps run in parallel.

## Rejected

*In-memory plans.* Die with the process; cannot be inspected or resumed.

*Assume mid-flight steps completed.* Optimistic, and how you get double-sent emails.
