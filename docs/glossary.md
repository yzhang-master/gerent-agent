# Glossary

Terms used precisely throughout these documents.

**Actor** — the identity work is done on behalf of. Present from day one though v1 is
single-user; it is the seam that becomes tenancy.

**Adapter** — a `ModelProvider` implementation translating between Gerent's normalized
types and one vendor's wire format. The only place vendor SDK types may appear.

**Agent CLI** — a complete external agent with its own loop and tools (Codex, Claude
Code). Plugs in as a *skill*, never as a provider. See
[providers.md](providers.md#two-kinds-of-backend).

**Assumption** — a decision the agent made unilaterally where a gated agent would have
asked. Recorded with rationale and confidence; always surfaced in the report.

**Block** — one unit of message content: text, thinking, tool call, tool result, image.
Normalized; never vendor-native.

**Budget** — a ceiling on a run: wall-clock, tokens, cost, or tool calls. Exhaustion
ends the run cleanly *with* a report.

**Capabilities** — a provider's feature descriptor, read by the kernel to decide what to
use and what to degrade.

**Checkpoint** — a restore point taken before an irreversible action. A git scratch ref
inside a repo, a content-addressed snapshot outside one.

**Degradation** — using a fallback because the active provider lacks a capability.
Always recorded in the report.

**Domain pack** — a drop-in directory of skills under `domains/`, discovered at startup.
How the agent becomes domain-wide.

**Guardrail** — a non-blocking safety mechanism: sandbox roots, hard denies, budgets,
kill switch. Replaces approval prompts.

**Journal** — the append-only structured record of a run, written as it proceeds. The
substrate the report is rendered from.

**Kernel** — the loop: reason, plan, act, report. Knows nothing about which port
produced the turn.

**Plan / Step** — a durable DAG in Postgres. There is no `blocked` state.

**Port** — an interface that produces `TurnRequest` and renders `TurnEvent`: voice, CLI,
HTTP/WS, scheduler.

**Report** — the rendered account of a run. For an autonomous agent, the primary
interface rather than a nicety.

**Resident set** — the small group of always-loaded skills. Everything else is deferred
or retrieved.

**Risk** — a skill's declared hazard class: `READ`, `WRITE`, `DESTRUCTIVE`, `EXTERNAL`.
Drives checkpointing.

**Role** — what a model call is *for* (`planner`, `worker`, `cheap`, `coder`), resolved
by the router to a provider chain. Callers ask for roles, never models.

**Run** — one goal worked to completion, possibly spanning many turns and steps.

**Skill** — an invokable capability. The domain-wide seam.

**TurnRequest / TurnEvent** — the two vocabularies that cross every boundary: what comes
in, and what streams out.
