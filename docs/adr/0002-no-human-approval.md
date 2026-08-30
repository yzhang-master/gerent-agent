# 0002 — No human approval; reversibility instead

**Status:** Accepted

## Context

The agent has shell access, filesystem write access, and a scheduler that fires runs
unattended. The conventional safety model is a confirmation prompt before dangerous
actions.

The product requirement is explicit: it should work like a human employee — take a
goal, do the whole job, report at the end. A prompt every few tool calls is not that,
and at 03:00 there is nobody to answer it.

## Decision

Remove approval prompts entirely. Replace them with four non-blocking mechanisms:

1. sandboxed workspace roots, checked after symlink resolution
2. mandatory checkpoints before anything irreversible, with restore commands in the
   report
3. a short hard-deny list that refuses outright and returns a `tool_result`, never an
   exception
4. per-run budgets and a kill switch, both of which end the run *with* a report

Ambiguity is resolved by deciding and recording an `Assumption`, not by asking.

## Consequences

**Gained:** the agent can actually run unattended; scheduled work is possible; no
half-finished runs waiting on a human who went to lunch.

**Paid, and stated plainly:** mistakes execute. A wrong `rm` or a bad refactor happens,
and is corrected afterward rather than prevented. Checkpoints cover the filesystem and
nothing else — a sent email, a dropped table, or a posted webhook is not recoverable.
A confident wrong assumption early in a plan can waste an entire run.

**Enforced by:** tests asserting no code path waits on human input; that every
`reversible=False` skill checkpoints; that a denial returns a result rather than
raising; and that budget exhaustion still produces a report.

## Rejected

*Approval prompts.* Incompatible with the requirement.

*A risk-scored policy engine with an auto-approve threshold.* This is approval with
extra steps: the threshold needs tuning, tuning needs exceptions, exceptions need
someone to grant them. The short hard-deny list is deliberately not extensible for this
reason.
