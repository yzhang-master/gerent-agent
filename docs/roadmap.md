# Roadmap

Milestones are ordered so that each one produces something that runs. "Done" below
means demonstrable, not written.

## M0 — It talks, through two providers

uv project, config, event bus, normalized types, `ModelProvider` ABC, **Anthropic and
OpenRouter adapters**, router, CLI port.

**Done when:** `gerent chat --provider anthropic` and `--provider openrouter` both
stream a real reply through the same code path.

Two providers here is deliberate and not negotiable. An abstraction validated against a
single backend is that backend's API with extra indirection; the second one is where
you discover what the interface actually has to be. Doing this at M0 costs a day. Doing
it at M4 costs a rewrite of everything built on top.

## M1 — It acts, reversibly

`Skill` ABC, registry + domain discovery, per-provider tool rendering, guardrails,
checkpoints, and the built-in shell/files/code/web skills.

**Done when:** the agent edits files and runs commands unattended on either provider,
and a destructive operation auto-checkpoints and is provably restorable.

Guardrails land here — before the agent has meaningful reach, and three milestones
before the scheduler starts firing runs at 03:00.

## M2 — It remembers

Postgres pool, migrations, episodic + semantic memory, tool-retrieval fallback.

**Done when:** the process restarts and the agent recalls the prior session; and the
tool surface stays bounded on a provider without native deferred loading.

## M3 — It finishes a job and reports

Plan/Step models, planner, durable executor, journal, reporter, `delegate` skill.

**Done when:** `gerent do "<goal>"` runs start to finish with zero prompts and prints a
result report; the run survives `kill -9` mid-plan and resumes; a coding task can be
subcontracted to Codex.

**M0–M3 is the first coherent product: give it a goal, walk away, read the report.**
Everything after this adds surfaces to something that already works.

## M4 — It runs on its own schedule

`schedules` table, poller, `schedule` skill, pushed reports.

**Done when:** "every weekday at 09:00, summarize my repo" fires at the right local
time across a DST boundary and delivers its report to the configured sink.

## M5 — It listens and speaks

VAD, STT/TTS providers, duplex pipeline, barge-in.

**Done when:** a full spoken conversation works, interrupting mid-sentence stops the
agent, and the truncated reply is what gets recorded in history.

## M6 — It serves

FastAPI + WebSocket port, `actor`/tenancy plumbed end to end.

**Done when:** a browser client drives the same kernel with no kernel changes.

Needs Docker, which is not installed on the target machine.

---

## Ordering rationale

Voice is the most demonstrable feature and deliberately fifth. It is a *port* — it adds
no capability the agent does not already have, and building it before the agent can
reliably finish a job produces something that talks impressively and accomplishes
little.

The scheduler is fourth for a safety reason rather than a technical one: it is the
component that runs the agent while nobody is watching, and it should not exist until
guardrails (M1) and honest reporting (M3) do.

## Out of scope for now

Multi-agent orchestration, a GUI, mobile clients, fine-tuning, and a plugin marketplace.
Each is defensible later; none of them helps the agent finish a job today.
