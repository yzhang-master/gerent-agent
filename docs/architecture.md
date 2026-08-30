# Architecture

## The one idea

**Every input becomes the same request object and runs through the same loop.**

A spoken utterance, a typed line, a cron firing at 03:00, an inbound webhook — each is
normalized into a `TurnRequest` and handed to the kernel. The kernel does not know
which port produced it. Ports differ; the loop does not.

This is worth stating first because most of the structure below follows from it, and
because the obvious alternative — a chat loop with a voice mode bolted on and a
scheduler running its own separate logic — is how these systems usually end up with
three subtly different agents in one process.

```
  ports/                      kernel                        state
 ┌──────────┐              ┌───────────┐              ┌──────────────┐
 │ voice    │─┐            │  reason   │              │ Postgres     │
 │ cli      │─┼─TurnRequest│  plan     │◄────────────►│  sessions    │
 │ http/ws  │─┤     ──────►│  act      │              │  messages    │
 │ scheduler│─┘            │  report   │              │  plans/steps │
 └──────────┘              └─────┬─────┘              │  journal     │
       ▲                         │                    │  memories    │
       │                         ▼                    └──────────────┘
       └────TurnEvent──────┌───────────┐
            (streamed)     │ EventBus  │
                           └─────┬─────┘
            ┌────────────────────┼────────────────────┐
            ▼                    ▼                    ▼
    ┌───────────────┐    ┌──────────────┐    ┌─────────────────┐
    │ ModelRouter   │    │ SkillRegistry│    │ Guardrails      │
    │  anthropic    │    │  builtin/    │    │  + Checkpoints  │
    │  openai       │    │  domains/*/  │    └─────────────────┘
    │  openrouter   │    └──────────────┘
    │  ollama       │
    └───────────────┘
```

## The two vocabularies

The system has exactly two data types that cross every boundary. Everything else is
local to a module.

### `TurnRequest` — what comes in

```python
@dataclass
class TurnRequest:
    session_id: UUID
    source: Source          # VOICE | CLI | API | SCHEDULER | WEBHOOK
    text: str | None
    audio: AsyncIterator[bytes] | None
    locale: str | None      # BCP-47; None means "detect it"
    actor: Actor            # who this is on behalf of — the multi-tenant seam
    deadline: datetime | None
    budget: Budget | None   # tokens, wall-clock, cost
```

`actor` exists from day one even though v1 is single-user. Threading identity through
later means touching every function that handles a turn; carrying an unused field
costs nothing.

### `TurnEvent` — what goes out

One vocabulary, streamed, that every port knows how to render:

| Event | CLI renders as | Voice renders as | API renders as |
|---|---|---|---|
| `thinking` | dim italic line | filler phrase, or silence | SSE comment |
| `text_delta` | printed token | sentence-chunked TTS | SSE data |
| `tool_call` | `▸ shell(…)` | usually suppressed | SSE data |
| `tool_result` | collapsed output | suppressed | SSE data |
| `assumption` | `⚠ assumed: …` | spoken briefly | SSE data |
| `report` | rendered markdown | spoken summary | SSE data |
| `done` | prompt returns | pipeline re-arms | stream closes |

Adding a port means implementing this table, nothing more. That is the whole point.

## Kernel phases

The kernel is a small state machine over four phases. Most turns touch only two.

**reason** — Assemble context (working memory + retrieved memories + tool surface),
call the routed provider, stream events. See [providers.md](providers.md).

**plan** — Only for goals that need more than a couple of tool calls. Produces a
durable DAG in Postgres rather than an in-memory list, so the work survives a restart.
See [planning.md](planning.md).

**act** — Execute skills. Guardrails run first; checkpoints are taken before anything
irreversible. See [skills.md](skills.md) and [autonomy.md](autonomy.md).

**report** — Render the journal into a result the human reads. This is a phase, not a
side effect: for an agent that never interrupts, the report *is* the interface.
See [reporting.md](reporting.md).

## Module map

```
gerent/
  core/        config bus types errors kernel
  reasoning/   engine router effort trace
               providers/  base anthropic openai openrouter ollama
  planning/    models planner executor
  reporting/   journal reporter
  skills/      base registry guardrails checkpoint retrieval
               builtin/  shell files code web memory schedule delegate
  memory/      store episodic semantic working
  scheduler/   models service triggers
  voice/       pipeline vad  stt/{base,deepgram,whisper_local}  tts/{base,cartesia,piper_local}
  i18n/        locale
  ports/       cli api
  db/          pool  migrations/
domains/       drop-in skill packs, discovered at runtime
tests/         conformance/  unit/  integration/
```

## Dependency rule

Dependencies point inward. `core` depends on nothing else in the project.

```
ports ──► kernel ──► reasoning ──► providers ──► (vendor SDKs)
              │           │
              ├──► skills ┤
              ├──► memory ┤
              └──► core ◄─┘
```

Two rules a reviewer should enforce mechanically:

1. **No vendor SDK type may appear outside `reasoning/providers/`.** Not in the kernel,
   not in the database, not in a skill. The moment an `anthropic.types.Message` is
   persisted, the schema is welded to one vendor and cross-provider failover is dead.
2. **No skill may import the kernel.** Skills receive a `SkillContext`; they do not
   reach back into the loop that called them.

## What runs where

v1 is a single process with several asyncio tasks: the kernel, the scheduler poller,
and — when active — the voice pipeline. Postgres is the only external dependency.

The seams that make this a service later, without a rewrite:

- `actor` on every request → tenancy
- Postgres for all state, nothing important in process memory → horizontal scale
- `EventBus` as an interface → swap in-process pub/sub for Redis/NATS
- ports already speak `TurnRequest`/`TurnEvent` → a WebSocket port is just another port

None of that is built in v1. All of it is *possible* in v1 because of where the lines
are drawn.

## Where the risk actually is

Three things in this architecture are hard to change after the fact, and they are the
reason the docs came before the code:

1. **The normalized message format** ([providers.md](providers.md)) — it is written to
   the database. Getting it wrong means a migration of every stored conversation.
2. **The `Skill` contract** ([skills.md](skills.md)) — every domain pack ever written
   implements it.
3. **The absence of a human-approval step** ([autonomy.md](autonomy.md)) — designs
   built around "and then we ask the user" cannot have that removed later; the
   reversibility model has to be there from the start.

Everything else here can be rewritten in an afternoon.
