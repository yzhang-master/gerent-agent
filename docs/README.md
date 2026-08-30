# Gerent documentation

Written before the implementation, on purpose. The contracts described here are the
parts that are expensive or impossible to change later; the code around them is cheap.

## Reading order

**Start here**

1. [architecture.md](architecture.md) — the one idea the whole system rests on, and how a turn flows through it.

**The load-bearing contracts** — these three are the ones to get right on day one.

2. [providers.md](providers.md) — the vendor boundary: `ModelProvider`, normalized messages, capability negotiation, routing and failover, and why agent CLIs plug in somewhere else entirely.
3. [skills.md](skills.md) — the `Skill` contract, runtime discovery of domain packs, and how the tool surface stays bounded when there are 300 tools.
4. [autonomy.md](autonomy.md) — no approval prompts, and the reversibility model that replaces them.

**The rest of the system**

5. [planning.md](planning.md) — durable plan DAGs, resumption, and why there is no `blocked` state.
6. [reporting.md](reporting.md) — the journal and the run report. Autonomy's other half.
7. [memory.md](memory.md) — working, episodic, and semantic memory.
8. [scheduling.md](scheduling.md) — durable, timezone-correct triggers.
9. [voice.md](voice.md) — the duplex speech pipeline, barge-in, and the latency budget.

**Reference**

10. [data-model.md](data-model.md) — the Postgres schema.
11. [configuration.md](configuration.md) — every config key.
12. [glossary.md](glossary.md) — terms used precisely throughout these docs.
13. [roadmap.md](roadmap.md) — milestones and what "done" means for each.

**Decisions**

[adr/](adr/) — short records of the choices that were genuinely contested, and what
each one costs. Read these when a design here looks arbitrary; it usually isn't, and
the ADR says what the alternative was.

## Conventions in these documents

- Code blocks are **contracts**, not implementations. They show the shape a thing must
  have, not the body it will get.
- Where a decision has a real cost, the cost is stated. A design document that only
  lists advantages is marketing.
- "Must" means a test will enforce it. "Should" means a reviewer will argue about it.
