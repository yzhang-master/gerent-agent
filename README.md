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

**Pre-implementation.** This repo currently contains the design only. Documentation is
written first, deliberately: the contracts in `docs/` are the expensive things to get
wrong, and several of them (the normalized message format especially) are close to
impossible to retrofit.

See [docs/roadmap.md](docs/roadmap.md) for the build order. Nothing runs yet.

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
