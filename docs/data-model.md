# Data model

PostgreSQL 18. Every piece of state that matters lives here — nothing important is held
only in process memory. That is what makes crashes survivable, plans resumable, and a
second process possible later.

## Tables

```
actors          who work is done on behalf of (the tenancy seam)
sessions        a conversation or run
messages        normalized conversation history  ← provider-portable
plans           goal + status
steps           the DAG
assumptions     decisions made without asking
journal         append-only record of everything that happened
checkpoints     restore points
schedules       time triggers
memories        durable semantic facts
usage           per-call cost attribution
```

## The ones with sharp edges

### `messages`

```sql
CREATE TABLE messages (
    id          UUID PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id),
    seq         BIGINT NOT NULL,
    role        TEXT NOT NULL,        -- system | user | assistant | tool
    blocks      JSONB NOT NULL,       -- normalized Block[] — NEVER vendor-native
    provider    TEXT,                 -- which provider produced it (audit only)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);
```

**`blocks` holds Gerent's normalized format, never a vendor SDK's.** This is the single
most important constraint in the schema. Storing `anthropic.types.ContentBlock` here
would weld the database to one vendor and make mid-conversation failover impossible,
because the history could not be re-rendered for a different provider. See
[providers.md](providers.md#normalized-types).

`provider` is recorded for audit and cost attribution — not so that rendering can
branch on it.

### `steps`

```sql
CREATE TABLE steps (
    id             UUID PRIMARY KEY,
    plan_id        UUID NOT NULL REFERENCES plans(id),
    seq            INT  NOT NULL,
    description    TEXT NOT NULL,
    depends_on     UUID[] NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,     -- pending|running|done|failed|skipped
    attempts       INT  NOT NULL DEFAULT 0,
    result         TEXT,
    checkpoint_ref TEXT,
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ
);
CREATE INDEX ON steps (plan_id, status);
```

No `blocked` status, deliberately — see [planning.md](planning.md#there-is-no-blocked-state).

Steps found in `running` at startup died mid-execution and are ambiguous: re-run if
idempotent, otherwise fail and replan.

### `journal`

```sql
CREATE TABLE journal (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL,
    step_id    UUID,
    kind       TEXT NOT NULL,     -- tool_call | assumption | checkpoint | degradation | ...
    payload    JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON journal (run_id, id);
```

Append-only, written during the run. Arguments inside `payload` are digested rather
than stored raw where they may carry secrets. This table is what the report is rendered
from — see [reporting.md](reporting.md).

### `memories`

```sql
CREATE TABLE memories (
    id            UUID PRIMARY KEY,
    actor_id      UUID NOT NULL REFERENCES actors(id),
    content       TEXT NOT NULL,
    embedding     VECTOR(1024),          -- nullable: pgvector may be absent
    tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    superseded_by UUID REFERENCES memories(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**pgvector is not assumed.** If `CREATE EXTENSION vector` is unavailable, `embedding`
stays null and retrieval uses the `tsv` full-text index behind the same interface —
worse recall, entirely workable for v1.

`superseded_by` rather than deletion: when a fact is replaced, the old one stays
inspectable. Useful precisely when the agent is behaving oddly and you want to know
what it believed.

`actor_id` is filtered on every retrieval from day one, even though v1 is single-user.
A cross-tenant memory leak is not a bug to discover after adding tenancy.

### `schedules`

`timezone` is an **IANA name**, never a UTC offset; `next_fire_at` is UTC, computed by
interpreting the expression in that zone. See
[scheduling.md](scheduling.md#timezones) for the DST rules this exists to get right.

## Conventions

- UUIDv7 for entity keys — time-ordered, so index locality comes free.
- `TIMESTAMPTZ` everywhere. There is no naive timestamp in this schema.
- JSONB for anything shaped by the model (`blocks`, `payload`); real columns for
  anything queried or filtered.
- Foreign keys everywhere they exist logically. Constraints are cheaper than debugging
  orphans.

## Migrations

Plain numbered SQL in `db/migrations/`, applied in order, forward-only.

```
0001_init.sql
0002_add_usage.sql
```

No ORM and no migration framework in v1: the schema is small, the queries are explicit,
and `asyncpg` against hand-written SQL is faster to reason about than a mapping layer.
That preference is worth revisiting if the schema grows past roughly twenty tables.

## Retention

`messages` and `journal` grow without bound. v1 does not prune them — a single-user
agent will not fill a disk in a year, and premature retention policy destroys exactly
the history that makes incidents debuggable. Revisit before multi-tenant deployment,
where the arithmetic changes completely.
