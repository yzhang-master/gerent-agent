# 0001 — Normalized message format, not vendor-native

**Status:** Accepted

## Context

Conversation history is persisted and replayed on every turn. It could be stored in the
active provider's native block format, or in a format of our own.

Native storage is simpler and lossless: no translation layer, no fidelity questions,
and provider features work exactly as documented.

## Decision

Define Gerent's own `Msg`/`Block` types. Vendor types stop at the adapter boundary and
never appear in the kernel, the skills, or the database.

A `provider_meta` field carries vendor-specific data that must survive a round trip to
the *same* provider (thinking-block signatures being the motivating case). It is keyed
by provider and dropped when re-rendering to a different one.

## Consequences

**Gained:** mid-conversation failover between providers; a schema that does not need
migrating when a vendor changes its format; the ability to replay a stored conversation
against any backend.

**Paid:** a translation layer per adapter, and real fidelity loss — thinking blocks do
not survive a provider switch, vendor-specific stop reasons collapse into a shared
enum, and cache breakpoints are re-derived rather than carried across.

**Enforced by:** a conformance test asserting `Msg` → provider format → `Msg` preserves
semantics, and a review rule that no vendor SDK import may appear outside
`reasoning/providers/`.

## Rejected

*Vendor-native storage.* Buys fidelity and simplicity, forfeits portability and
failover, and welds the database schema to one vendor. For a system whose premise is
that the vendor is swappable, that trade is backwards.

*Dual storage (native + normalized).* Doubles the write path and creates two sources of
truth that will diverge.
