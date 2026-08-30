# Memory

Three kinds, with different lifetimes and different failure modes. Conflating them is
the usual mistake — "memory" as one undifferentiated store is either too small to be
useful or too large to fit in a context window.

| Kind | Lifetime | Holds | Retrieval |
|---|---|---|---|
| **Working** | One turn | Assembled context sent to the model | Constructed per turn |
| **Episodic** | Forever | What happened — messages, journal entries | By session, recency |
| **Semantic** | Forever | What is true — facts, preferences, learned constraints | By similarity to the turn |

## Working memory

The context assembled for a single model call. Not stored; rebuilt every turn, in
render order:

```
[ system prompt (frozen, cacheable) ]
[ tool surface (resident + retrieved, deterministically sorted) ]
[ retrieved semantic memories ]
[ session history — recent turns verbatim, older turns summarized ]
[ plan context — goal, current step, dependency results ]
[ current input ]
[ volatile: current time, timezone, locale ]
```

Two constraints drive that ordering, both from [providers.md](providers.md):

- **Stable content first.** Prompt caching is prefix-based; any byte change invalidates
  everything after it. The frozen system prompt and a *deterministically sorted* tool
  list go first.
- **Volatile content last.** Current time changes every turn and would invalidate the
  cache on every request if placed early. Where the provider supports it, time and
  locale are injected as a mid-conversation system message rather than in the cached
  prefix.

If cache hit rate is flat zero across turns, something is invalidating the prefix — an
unsorted tool list and a timestamp in the system prompt are the two usual culprits.

## Episodic memory

The record of what happened: `messages` (normalized blocks, replayable) and `journal`.
Append-only. This is what makes a session resumable after a restart and auditable
afterward.

Growth is handled by summarization, not deletion: older turns collapse into summaries
while the full rows stay in Postgres. The context window is a working set; the database
is the record.

## Semantic memory

Durable facts worth carrying across sessions: preferences ("deploys go to staging
first"), constraints ("this repo uses pnpm, not npm"), and learned corrections.

**Writing.** Two paths — the agent calls `remember` deliberately, or an extraction pass
over a finished run proposes candidates. Extraction is the useful one and also the
dangerous one: an agent that stores everything it sees fills semantic memory with
transient noise ("the user is currently debugging test_foo") that pollutes retrieval
for months. Bias hard toward storing few, durable, general facts.

**Retrieval.** Top-K by similarity to the current turn, injected into working memory.
pgvector where available; **Postgres full-text search where not** — the same interface,
somewhat worse recall, and a perfectly adequate v1. pgvector is not assumed to be
installed.

**Conflict.** Facts contradict each other over time ("uses npm" → "uses pnpm"). Newer
supersedes older; the superseded row is marked, not deleted, so the history of what the
agent believed remains inspectable when it behaves oddly.

**Scoping.** Every memory carries `actor`. Retrieval filters on it from day one, even
though v1 is single-user — a cross-tenant memory leak is not a bug you want to discover
after adding tenancy.

## Skills

| Skill | Effect |
|---|---|
| `remember(fact, scope)` | Write a durable fact |
| `recall(query)` | Explicit search, beyond automatic retrieval |
| `forget(id)` | Mark superseded |

Automatic retrieval covers most cases; `recall` exists for when the agent knows it
needs something specific that similarity search would not surface.

## Honest limits

- Retrieval quality is the ceiling on the whole system. A fact stored in words unlike
  those the user later uses is invisible, and looks like forgetting.
- Summarization loses detail irreversibly *from context* — though never from Postgres.
- Semantic memory is a small durable set, not an archive. Anything that needs complete
  recall belongs in a skill that queries the source of truth.
