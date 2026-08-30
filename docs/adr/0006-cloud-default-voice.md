# 0006 — Cloud speech by default, local as fallback

**Status:** Accepted

## Context

Speech recognition and synthesis can run locally or through streaming cloud APIs. Local
is free, private, and offline. Cloud is faster, better, and multilingual out of the box.

The deciding constraint is the target machine: **8 CPU cores, 23 GB RAM, no GPU.**

## Decision

Define `STTProvider` and `TTSProvider` interfaces with both backends behind them. Ship
cloud (Deepgram, Cartesia) as the default; ship local (`faster-whisper`, Piper) as the
offline fallback.

## Consequences

**Gained:** a ~1.3 s round-trip budget is achievable, and ~90 languages work without
per-language setup — which is most of what "world-wide" means in practice. The
interface means the choice is config, not architecture.

**Paid:** audio leaves the machine, there is a per-minute cost, and voice stops working
without a network. The local fallback is honestly worse rather than equivalent:
`faster-whisper base` is roughly realtime on this CPU, `small` is not, and Piper is
intelligible but robotic.

## Rejected

*Local-only.* On a GPU-less box the latency makes conversation unpleasant and the
language coverage collapses.

*Cloud-only.* Forecloses offline and privacy-sensitive use for no structural gain — the
interface costs almost nothing once both sides stream.
