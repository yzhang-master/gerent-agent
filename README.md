# Gerent

An autonomous, provider-agnostic AI agent that works like a colleague: you give it a
goal, it does the whole job, and it hands you a report.

*Gerent* (n.) — one who conducts affairs on another's behalf.

## What it is

A general-purpose agent core whose competences are **reasoning, planning, speech
recognition, speaking, coding, and scheduling** — and which is deliberately not tied to
one problem domain, one language, or one model vendor.

Four properties shape every design decision in this repo:

| Property | Meaning |
|---|---|
| **Domain-wide** | Capability is not compiled into the core. Domains arrive as drop-in skill packs discovered at runtime. Adding a domain is adding a directory. |
| **World-wide** | Multilingual by construction (streaming ASR/TTS), timezone-correct scheduling, deployable from a laptop daemon to a multi-tenant service without a rewrite. |
| **Autonomous** | It does not ask permission mid-task. It decides, records the decision, finishes, and reports. Safety comes from reversibility, not from prompts. |
| **Provider-agnostic** | Claude, OpenAI, OpenRouter, local models, and agent CLIs (Codex, Claude Code) are interchangeable backends. No vendor SDK type appears above the adapter layer. |

## The shape of it

Every input — a spoken sentence, a CLI line, a cron firing, a webhook — becomes the
same `TurnRequest` and runs through the same kernel. Voice is not a special mode. The
scheduler is not a separate program. The model vendor is a config value.

```
ports ──► TurnRequest ──► Kernel (reason · plan · act · report) ──► TurnEvent ──► ports
                              │
                    ┌─────────┼─────────┐
              ModelRouter  Skills   Guardrails
```

## Status

Runs. M0-M4 are implemented and tested; M5 is partial; M6 is not started.

| Milestone | State |
|---|---|
| M0 provider boundary, router, CLI | done |
| M1 skills, guardrails, checkpoints, tool loop | done |
| M2 semantic memory with automatic retrieval | done |
| M3 durable plans, executor, journal, reporter, delegate | done |
| M4 scheduler, timezone-correct triggers | done |
| M5 voice | **partial** - pipeline, VAD, chunking and barge-in are done and tested; the Deepgram and Cartesia adapters are not written |
| M6 HTTP/WebSocket port | not started |

39 tests, all driven through scripted providers, so the suite costs nothing to run.

**Not yet verified against a live model.** There are no provider credentials on this
machine, so every test runs against a fake. The adapters are written to the documented
APIs but have not made a real request - treat the first live run as the real smoke test.

Two things needed before a real run: credentials for at least one provider, and (for
durable plans) a Postgres role. Without a DSN the agent still runs, and tells you plainly
that plans are held in memory.

```bash
uv venv && uv pip install -e ".[anthropic,openai,db,dev]"
cp gerent.example.toml gerent.toml
uv run gerent doctor                      # what is configured, reachable, missing
uv run gerent chat "summarise this repo"
uv run gerent do "add a test for the parser and run the suite"
```

## Documentation

Start at **[docs/README.md](docs/README.md)**.

The two documents that matter most before any code is written are
[docs/providers.md](docs/providers.md) (the vendor boundary) and
[docs/autonomy.md](docs/autonomy.md) (what replaces human approval).

## Intended environment

Python 3.12 + uv, PostgreSQL 18, ffmpeg. Runs as a single local daemon; every internal
boundary is drawn so it can become a service later. No GPU assumed — which is why the
default speech stack is streaming cloud APIs with local CPU models as the offline
fallback.
