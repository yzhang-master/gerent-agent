# Planning

Planning is what separates "answer a question" from "do a job." A goal that needs
twelve tool calls across twenty minutes cannot be held in a single model turn, and it
must survive the process dying halfway through.

## Durable by default

A plan is a **DAG persisted in Postgres**, not a list in memory.

```python
@dataclass
class Plan:
    id: UUID
    session_id: UUID
    goal: str
    status: PlanStatus          # running | done | failed | abandoned
    created_at: datetime

@dataclass
class Step:
    id: UUID
    plan_id: UUID
    seq: int
    description: str
    depends_on: list[UUID]
    status: StepStatus          # pending | running | done | failed | skipped
    attempts: int
    result: str | None
    checkpoint_ref: str | None
```

This is the difference between an agent that *plans* and an agent that *has a plan*.
In-memory plans die with the process, cannot be inspected while running, and cannot be
resumed by a scheduler firing four hours later. All three of those are requirements
here.

## There is no `blocked` state

Deliberately. A gated agent blocks on ambiguity and waits for a human; Gerent decides,
records an `Assumption`, and continues. See
[autonomy.md](autonomy.md#deciding-instead-of-asking).

The state machine is therefore small:

```
pending ──► running ──► done
               │
               ├──► failed ──► (replan) ──► pending
               └──► skipped        (dependency failed, or made unnecessary)
```

`skipped` matters for the report: work that was planned and then turned out to be
unnecessary is a legitimate outcome, and hiding it makes the report a worse account of
what happened.

## Planning is not always worth it

Most turns should not produce a plan. The planner runs when a goal is multi-step,
long-running, or likely to need recovery; a single-question turn goes straight to the
`worker` role and answers.

The heuristic is cheap and deliberately biased toward *not* planning: a `cheap`-role
classification decides, and ambiguous cases skip the planner. Over-planning is a real
cost — it doubles latency on trivial requests and produces four-step plans for things
that were one step.

## The loop

```
planner  ──► Plan(goal) ──► steps in Postgres
                                  │
executor ◄────────────────────────┘
   │  pick ready steps (deps satisfied)
   │  run each as a normal kernel turn, with skills
   │  persist result + status after every step
   │  on failure: retry, then replan, then fail the step
   └► when no ready steps remain: report
```

Two properties the executor must hold:

- **Persist after every step.** Not at the end, not on a timer. The point of durability
  is a crash between any two steps.
- **Run steps as ordinary turns.** A step is not a special execution mode; it is a
  `TurnRequest` with plan context attached. One loop, again.

Independent ready steps may run concurrently, bounded by config. Concurrency interacts
badly with checkpoints — two parallel steps checkpointing the same workspace produce an
ambiguous restore point — so **steps that touch the filesystem are serialized**, and
only read-only or external steps run in parallel. That is a real limit on throughput,
accepted in exchange for restores that mean something.

## Failure and replanning

Escalating, with a hard stop:

1. **Retry** — transient failures (timeout, 429, flaky network). Bounded, with backoff.
2. **Replan** — the step was wrong or its premise was invalid. The planner sees the
   failure and the journal, and rewrites the remaining DAG. Bounded by
   `max_replans`, default 3.
3. **Fail the step** — mark it `failed`, skip its dependents, continue with anything
   independent. A partial result plus an honest report beats abandoning the run.

The bound on replanning is what stops the classic failure mode: an agent that replans
forever, each time confidently, spending the entire budget on a task that was
impossible from the start.

## Resumption

On startup, the executor looks for plans in `running` and resumes them.

- Steps in `done` are not re-run.
- Steps caught in `running` at crash time are ambiguous — the process died mid-step and
  cannot know whether the side effect landed. They are **re-run if the step is
  idempotent, otherwise failed and replanned.** Guessing "it probably finished" is how
  you get double-sent emails.
- Checkpoints from before the crash remain valid restore points.

This is what makes `kill -9` a survivable event rather than a lost afternoon, and it is
what lets a scheduled run pick up work a previous run left unfinished.

## Context across steps

A long plan will exceed any context window, so what carries between steps is curated,
not accumulated:

- the goal and the current step
- results of steps this one depends on — **summarized, not verbatim**
- relevant retrieved memories ([memory.md](memory.md))
- assumptions made so far, so later steps stay consistent with earlier decisions

Where the provider supports server-side compaction it is used; otherwise Gerent
summarizes into the journal. Either way, the full history stays in Postgres — the
context window is a working set, not the record.
