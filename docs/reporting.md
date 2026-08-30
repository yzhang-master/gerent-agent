# Journal and reporting

An agent that never interrupts must account for itself afterward, or it is
unauditable. The report is not logging and not a nicety — for an autonomous worker it
**is the interface**.

## Journal

Append-only, structured, written as the run proceeds. Every entry carries
`run_id`, `step_id`, timestamp, and a typed payload.

| Entry | Recorded when |
|---|---|
| `goal` | The run starts |
| `plan` / `replan` | A DAG is created or rewritten |
| `step_start` / `step_end` | Step boundaries, with status |
| `tool_call` | Skill, argument digest, guardrail decision |
| `tool_result` | Outcome, duration, truncated output |
| `assumption` | An ambiguity was resolved unilaterally |
| `checkpoint` | Ref plus the exact restore command |
| `degradation` | A provider capability was unavailable and a fallback was used |
| `model_call` | Provider, model, role, usage, cost |
| `denial` | A guardrail refused something |
| `error` | Anything that failed |

Two rules: **arguments are digested, not stored raw**, where they might carry secrets;
and the journal is written *during* the run, never reconstructed at the end. A journal
assembled afterward from memory is a story, not a record — and it does not exist at all
if the process dies.

## The report

`reporting/reporter.py` renders the journal into what a human reads. Fixed structure,
in this order:

```markdown
# <goal>
<one-paragraph outcome: what happened, in plain language>

## What I did
<narrative of the actual work — steps, in order, with outcomes>

## What changed
| Path | Change | Diff |
|------|--------|------|
<every file touched, with a link to the diff>

## Assumptions I made
<question → what was chosen → why. Lowest confidence first.>

## What didn't work
<failures, skipped steps, denied actions, and what I did instead>

## Cost
<duration · tokens · USD · per provider/model>

## How to undo this
```
git restore --source=refs/gerent/<run-id>/3 -- .
```
```

### Rules the renderer enforces

**Failures are as prominent as successes.** A report that lists only wins is broken. If
three steps failed, the report says so above the fold, not in a footnote.

**Assumptions always appear**, even when trivial. They are the audit trail for every
decision a human would otherwise have been asked to make; suppressing the "obvious"
ones is how a wrong one slips through.

**The undo section is mandatory** whenever a checkpoint was taken. Reversibility that
the human cannot find is not reversibility — see
[autonomy.md](autonomy.md#2-checkpoint-before-irreversible).

**Degradations are stated.** "Tool search unavailable on `ollama:qwen3`; used retrieval
over 12 skills" turns a mysteriously poor run into a diagnosable one.

**Length tracks the work.** A one-tool-call turn does not get eight headed sections; it
gets a paragraph. The full structure is for runs that did real work.

## Rendering per port

The report is a `TurnEvent.report` carrying structured data. Each port renders it:

| Port | Rendering |
|---|---|
| CLI | Full markdown, syntax-highlighted |
| Voice | Spoken summary — outcome, count of changes, any low-confidence assumption. Never reads a diff aloud. |
| API / WS | JSON payload |
| Scheduler | **Pushed** to the configured sink (file, webhook, email) |

The scheduler row is the one that matters most. An unattended run whose report lands in
a logfile nobody opens is indistinguishable from a run that never happened.

## Failure reporting

Every terminal condition still produces a report — this is the property that makes the
whole thing trustworthy:

| Ending | Report |
|---|---|
| Success | Full report |
| Partial (some steps failed) | Full report; failures foregrounded |
| Budget exhausted | Partial report + what remained |
| Kill switch / SIGTERM | Partial report as of the last completed step |
| Provider refusal / all failover exhausted | Report explaining the run could not proceed |
| `SIGKILL` | **No report** — the plan survives in Postgres and resumes; see [planning.md](planning.md#resumption) |

Only the last row produces silence, and it is the one case the process cannot control.
Everything else reports.
