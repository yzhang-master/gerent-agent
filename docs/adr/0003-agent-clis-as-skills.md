# 0003 — Agent CLIs plug in as skills, not providers

**Status:** Accepted

## Context

The provider requirement named "Codex, Claude, OpenRouter" together. But these are not
the same kind of thing. Claude and OpenRouter expose completion endpoints; Codex CLI
and Claude Code are complete agents with their own loop, tool set, and working
directory.

The obvious reading — make them all `ModelProvider` implementations — would nest two
agent loops: Gerent deciding which tool to call while the sub-agent independently
decides which file to edit, neither aware of the other's plan, budget, or guardrails.

## Decision

Split by what owns the loop.

- **Model APIs** implement `ModelProvider` and drive Gerent's loop.
- **Agent CLIs** are exposed through a `delegate` skill: hand over a scoped task and a
  workspace path, stream output into the journal, capture the resulting diff as a
  `SkillResult`.

## Consequences

**Gained:** one loop with one budget and one set of guardrails. The delegate is
sandboxed and checkpointed like any other skill, so a subcontractor's mistake is
exactly as reversible as the agent's own. It also matches the product framing — a
competent worker hands a specialist a scoped task and stays accountable for the result.

**Paid:** the sub-agent's context, model choice, and token spend are invisible to
Gerent, so its cost does not appear in the run budget. Delegated tasks can run long
with little feedback. Granularity must be coarse — whole coherent tasks, not individual
steps.

## Rejected

*Agent CLIs as `ModelProvider`.* Nested loops with no shared budget or guardrails.

*A separate "sub-agent" subsystem alongside skills.* A second execution path needing its
own guardrails, journaling, and checkpointing — all of which the skill path already has.
