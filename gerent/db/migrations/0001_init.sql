-- Gerent initial schema. Forward-only, applied in order. See docs/data-model.md.

CREATE TABLE IF NOT EXISTS actors (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id          UUID PRIMARY KEY,
    actor_id    UUID REFERENCES actors(id),
    source      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- blocks holds Gerent's NORMALIZED block format, never a vendor SDK's. Storing native
-- blocks would weld this schema to one vendor and make failover impossible. ADR 0001.
CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         BIGINT NOT NULL,
    role        TEXT NOT NULL,
    blocks      JSONB NOT NULL,
    provider    TEXT,                       -- audit and cost attribution only
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS plans (
    id          UUID PRIMARY KEY,
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,              -- running|done|failed|abandoned
    replans     INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_status_idx ON plans (status);

-- No `blocked` status, deliberately: a gated agent blocks on ambiguity, a human worker
-- decides and records an assumption. See docs/planning.md.
CREATE TABLE IF NOT EXISTS steps (
    id             UUID PRIMARY KEY,
    plan_id        UUID NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    seq            INT NOT NULL,
    description    TEXT NOT NULL,
    depends_on     UUID[] NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,           -- pending|running|done|failed|skipped
    idempotent     BOOLEAN NOT NULL DEFAULT false,
    attempts       INT NOT NULL DEFAULT 0,
    result         TEXT,
    checkpoint_ref TEXT,
    started_at     TIMESTAMPTZ,
    ended_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS steps_plan_status_idx ON steps (plan_id, status);

CREATE TABLE IF NOT EXISTS assumptions (
    id          UUID PRIMARY KEY,
    plan_id     UUID REFERENCES plans(id) ON DELETE CASCADE,
    step_id     UUID REFERENCES steps(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    chosen      TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.5,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS journal (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL,
    step_id    UUID,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL,              -- secret-looking values already digested
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS journal_run_idx ON journal (run_id, id);

CREATE TABLE IF NOT EXISTS checkpoints (
    id              UUID PRIMARY KEY,
    run_id          UUID NOT NULL,
    kind            TEXT NOT NULL,          -- git|snapshot
    ref             TEXT NOT NULL,
    restore_command TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- timezone is an IANA name, never an offset; next_fire_at is UTC computed in that zone.
CREATE TABLE IF NOT EXISTS schedules (
    id            UUID PRIMARY KEY,
    actor_id      UUID REFERENCES actors(id),
    kind          TEXT NOT NULL,            -- cron|once|interval
    expr          TEXT NOT NULL,
    timezone      TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    report_sink   TEXT,
    retry_depth   INT NOT NULL DEFAULT 0,
    next_fire_at  TIMESTAMPTZ,
    last_fired_at TIMESTAMPTZ,
    running       BOOLEAN NOT NULL DEFAULT false,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules (enabled, next_fire_at);

-- Superseded rather than deleted: what the agent believed stays inspectable when it
-- behaves oddly. actor_id is filtered on every retrieval, from day one.
CREATE TABLE IF NOT EXISTS memories (
    id            UUID PRIMARY KEY,
    actor_id      UUID NOT NULL REFERENCES actors(id),
    content       TEXT NOT NULL,
    tsv           TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    superseded_by UUID REFERENCES memories(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memories_tsv_idx ON memories USING GIN (tsv);
CREATE INDEX IF NOT EXISTS memories_actor_idx ON memories (actor_id) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS usage (
    id           BIGSERIAL PRIMARY KEY,
    run_id       UUID NOT NULL,
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    role         TEXT NOT NULL,
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cache_read_tokens INT NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
